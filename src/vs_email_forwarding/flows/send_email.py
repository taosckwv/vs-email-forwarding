#!/usr/bin/env python3
"""流② 发信：认领 → SMTP 投递 → 条件 UPDATE（`specs/0001-email-forwarding-service` T-24）。

设计真源：``design.md`` §3.3（流② 的状态落点表与处置表）、§2.3（状态机）、§2.4（重试与错误列）、
§4.3 / §4.5（发信接口与配置来源）；认领与收割支口径见 ``plan.md`` §5.2、§5.3，确定性造态口径见
``plan.md`` §7.4。``AC-7`` 的集成落点在阶段 5（T-35）——按 ``plan.md`` §7.1 的分工，流的推进与
幂等属集成用例，本模块不带单测。

一次 tick 恒为三段，各开一个短会话，事务不跨外部调用（``docs/architecture.md`` §5）：

* **阶段① 认领**（短事务）：``db/repositories/messages.py`` 用 ``FOR UPDATE SKIP LOCKED`` 取一批
  出站 ``sending`` 且已到点的行；``attempt_count`` 与 ``next_attempt_at`` 在这一步写定，**提交之后**
  才发起投递。仓储逐行给出收割标志（``plan.md`` §5.3）：``is_harvest`` 为真的是「第 4 次尝试已
  认领、结果事务未提交」的崩溃遗留行，**不得**再发一次（第 5 次）投递。
* **阶段② 投递**（事务外）：逐行经 ``providers/smtp`` 的 ``SmtpSender`` 投一次；收割行跳过此步。
  单次调用受 ``SMTP_TIMEOUT_SECONDS`` 约束（``design.md`` §3 阶段②），``From`` 与 ``Reply-To``
  由适配器从注入的配置显式写入（``AGENTS.md`` §8 第 3 条）——**流层不拼发件人**。
* **阶段③ 结果**（第二个短事务）：条件 UPDATE 只写 ``status`` / ``last_error_class`` /
  ``last_smtp_code`` / ``sent_at``，**不写** ``attempt_count`` 与 ``next_attempt_at``
  （``design.md`` §3.3）：成功 → ``sent`` + ``sent_at``；失败 → 记分类与返回码，已达 4 次尝试
  则置终态 ``failed``，未达则保持 ``sending`` 等下一轮认领；收割支 → 直接置终态 ``failed``
  （``smtp_code`` 空）。``rowcount = 0``（起点已不是 ``sending``）按幂等忽略，不报错。

**【退避归属】** 4xx / 连接 / 超时类失败在本模块**不写** ``next_attempt_at``：退避由**认领事务**
写定——``design.md`` §2.4「``attempt_count`` 与 ``next_attempt_at`` 只在两处写：认领事务与目标行
建行时」、§3.3「未达上限则 ``status`` 保持 ``sending``（退避由认领时写好的 ``next_attempt_at``
承担）」、``plan.md`` §5.2 同口径；仓储 ``claim_due_send_messages`` 已按 ``constants`` 的退避表推
``now + backoff[n]``。因此本层的处置只有两种：未达上限 → 不落库（``RETRIED``，留待下一轮认领）、
已达上限或对端永久拒绝 → 条件 UPDATE 置终态。卡内 Objective 的「4xx 按 ``core/retry.next_attempt_at
(now, attempt)`` 推退避」指的正是这套算术，而它的**写入点**按上面三处真源落在认领事务：本层再写
一次即与「结果事务一律不碰这两列」冲突，且是同一个 ``now`` 的重复写。``core/retry.next_attempt_at``
目前没有生产调用方（仅 ``tests/unit/test_retry_policy.py`` 覆盖），这处归属已如实上报队长。

**【例外口径】** 本模块的调用面**没有**宽捕获：``providers/smtp/base.py`` 的接口契约要求适配器把
一次调用的结果（含连接、超时、对端拒绝）**全部收进返回值**、不抛异常（``smtplib_sender.py`` 用
``TimeoutError`` / ``OSError`` 两条具体类型兜住故障路径），而 ``design.md`` 里 ``internal`` 这条
分类只在审查环节出现（§2.4）。契约之外的异常因此交给监督循环（``docs/code-style.md`` §2.1 例外一，
落点 ``flows/scheduler.py::_run_loop``）留栈处理——它的错误文本与邮件内容无关，不触日志红线。
阶段③ 的数据库异常按 ``SQLAlchemyError`` 这一具体类型捕获：DB 错误原文可能回显行值（``CHECK``
违例的 ``DETAIL``），不能进监督循环的 ``logger.exception``。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast
from uuid import UUID

import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from vs_email_forwarding.config import Settings, build_tls_context
from vs_email_forwarding.constants import (
    ERROR_CLASS_INTERNAL,
    ERROR_CLASS_PERMANENT,
    MAX_ATTEMPTS,
    BodyFormat,
    ErrorClass,
)
from vs_email_forwarding.db.repositories.messages import (
    claim_due_send_messages,
    mark_send_failed,
    mark_sent,
)
from vs_email_forwarding.flows.cleanup import SessionFactory
from vs_email_forwarding.providers.smtp.base import OutboundMessage, SmtpDeliveryResult
from vs_email_forwarding.providers.smtp.smtplib_sender import SmtpConfig, SmtplibSender

__all__ = ["SendEmailResult", "send_email_tick"]

logger = structlog.get_logger(__name__)

# 受控取值一律从 constants 取具名常量，不在本模块重抄一遍（AGENTS.md §7）：对端永久拒绝取
# `permanent`（SMTP 侧只在 5xx 出现，design.md §3.3 处置表）；服务自身内部故障取 `internal`
# （收割支无对端结论可记时用它——见 _write_one 的注释）。
_PERMANENT = ERROR_CLASS_PERMANENT
_INTERNAL = ERROR_CLASS_INTERNAL


class _Outcome(StrEnum):
    """一行待发行在本次 tick 里的处置（内部词汇，不落库）。"""

    #: 对端接受 → ``status = sent`` 且写 ``sent_at``。
    SENT = "sent"
    #: 终态失败 → ``status = failed`` 且记 ``last_error_class`` / ``last_smtp_code``。
    FAILED = "failed"
    #: 崩溃遗留的第 4 次尝试 → 同样置终态 ``failed``，但**不产生第 5 次投递**。
    HARVESTED = "harvested"
    #: 临时故障且有剩余重试额度 → 不落库，退避已在认领时写好。
    RETRIED = "retried"


@dataclass(frozen=True, slots=True)
class SendEmailResult:
    """一次 tick 的处置计数（只统计**已提交**的结果；进程内使用，不落库）。

    四个字段与 :class:`_Outcome` 一一对应。阶段③ 提交失败时全部为 0——那一批决定一行都没提交，
    待写条数记在 ``send_email_result_failed`` 事件里。
    """

    sent: int = 0
    failed: int = 0
    harvested: int = 0
    retried: int = 0


@dataclass(frozen=True, slots=True)
class _ClaimedSend:
    """阶段① 收成的一行：仓储行转成中性对象，ORM 行不出 ``db/`` 的使用范围。"""

    message_id: UUID
    attempt_count: int
    is_harvest: bool
    message: OutboundMessage


@dataclass(frozen=True, slots=True)
class _Delivery:
    """阶段② 的产物：一行的处置决定，阶段③ 据此落库。

    ``error_class`` / ``smtp_code`` 只在终态失败时有值（``permanent``/``transient`` 来自适配器的
    受控判定，``internal`` 是本层对收割支的分类）。
    """

    message_id: UUID
    outcome: _Outcome
    error_class: ErrorClass | None = None
    smtp_code: int | None = None


async def send_email_tick(
    session_factory: SessionFactory,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> SendEmailResult:
    """执行一次流② tick：认领一批待发行、逐行投递、把结果条件更新落库。

    ``now`` 供测试注入（``plan.md`` §7.2：时间推进靠把 ``next_attempt_at`` 设成已到期，不靠
    sleep）；缺省取一次带时区的当前 UTC 时间，**本次 tick 的三个阶段共用这一个时刻**，
    谓词比较与 ``sent_at`` 因此不会漂移。

    认领阶段的数据库异常不在这里处理，交给监督循环（``docs/code-style.md`` §2.1 例外一）：这条路
    的错误文本只有方向、时刻与批量，不含邮件内容，留栈可查且不触日志红线。投递阶段的故障由适配器
    收成受控判定（``providers/smtp/base.py``），结果事务的数据库异常只记事件并返回零计数——单条流
    不会因一次 tick 打死自己（``plan.md`` §5.1）。
    """
    tick_now = now if now is not None else datetime.now(UTC)
    claimed = await _claim(session_factory, now=tick_now, batch_size=settings.batch_size)
    if not claimed:
        return SendEmailResult()
    deliveries = await _deliver_all(claimed, settings)
    result = await _write_results(session_factory, deliveries, now=tick_now)
    logger.info(
        "send_email_tick",
        sent=result.sent,
        failed=result.failed,
        harvested=result.harvested,
        retried=result.retried,
    )
    return result


async def _claim(
    session_factory: SessionFactory, *, now: datetime, batch_size: int
) -> list[_ClaimedSend]:
    """阶段① 认领（短事务）：取一批到点的待发行，提交后才发起投递。

    转换在会话作用域内完成（``design.md`` §8：只消费仓储返回的中性对象与自身需要的字段，不把
    ORM 行传到别的层或别的 tick）。认领事务必须在发起投递之前提交（``plan.md`` §5.1）：提交即
    释放行锁，SMTP 交互不在事务内进行。
    """
    async with session_factory() as session:
        claimed = await claim_due_send_messages(session, now=now, batch_size=batch_size)
        batch: list[_ClaimedSend] = []
        for row, is_harvest in claimed:
            batch.append(
                _ClaimedSend(
                    message_id=row.id,
                    # 认领后的 attempt_count 就是本次尝试的序号（1 ~ MAX_ATTEMPTS）：
                    # 阶段② 失败时用它判断还有没有重试额度。
                    attempt_count=row.attempt_count,
                    is_harvest=is_harvest,
                    message=OutboundMessage(
                        recipient=row.recipient,
                        subject=row.subject,
                        body=row.body,
                        # ORM 列是 str；表上的 CHECK 已把取值钉在 BODY_FORMATS 内（design.md §2.1）。
                        body_format=cast("BodyFormat", row.body_format),
                    ),
                )
            )
        await session.commit()
    return batch


async def _deliver_all(claimed: Sequence[_ClaimedSend], settings: Settings) -> list[_Delivery]:
    """阶段② 投递（事务外）：逐行投一次；收割行不投递，直接置终态。

    适配器在投递之前装配一次（``smtplib_sender.py``：本类不读配置，「装配」发生在 ``flows/``）：
    ``From`` / ``Reply-To`` 只来自配置，流层不拼发件人（``AGENTS.md`` §8 第 3 条）。
    """
    sender = _build_sender(settings)
    deliveries: list[_Delivery] = []
    for claim in claimed:
        if claim.is_harvest:
            # 收割支：不递增、不推退避、**不发起任何投递**——这就是「不产生第 5 次投递」
            # （plan.md §5.3 / AC-37）。分类留给阶段③ 记 internal（见 _write_one）。
            deliveries.append(_Delivery(message_id=claim.message_id, outcome=_Outcome.HARVESTED))
            continue
        delivered = await sender.send_message(
            claim.message, timeout_seconds=settings.smtp_timeout_seconds
        )
        deliveries.append(_decide(claim, delivered))
    return deliveries


def _decide(claim: _ClaimedSend, delivered: SmtpDeliveryResult) -> _Delivery:
    """把一次投递的受控判定翻成处置（``design.md`` §3.3 的处置表）。

    三行处置：接受 → ``sent``；``5xx``（permanent）→ 立即终态 ``failed``，不再消耗剩余重试次数
    （``AC-34``：第 1 次尝试即终态）；其余失败（``4xx`` 与连接 / 超时类故障，均为 transient）→
    未达 4 次尝试保持 ``sending`` 等下一轮认领，已达上限则终态 ``failed``（``AC-8``）。
    """
    if delivered.accepted:
        return _Delivery(message_id=claim.message_id, outcome=_Outcome.SENT)
    exhausted = claim.attempt_count >= MAX_ATTEMPTS
    if delivered.error_class == _PERMANENT or exhausted:
        return _Delivery(
            message_id=claim.message_id,
            outcome=_Outcome.FAILED,
            error_class=delivered.error_class,
            smtp_code=delivered.code,
        )
    # 未达上限：不落库——退避已在认领时写好，status 保持 sending（design.md §3.3）。
    return _Delivery(message_id=claim.message_id, outcome=_Outcome.RETRIED)


async def _write_results(
    session_factory: SessionFactory, deliveries: Sequence[_Delivery], *, now: datetime
) -> SendEmailResult:
    """阶段③ 结果（第二个短事务）：本批决定逐行条件更新，最后统一提交。

    一批一个短会话（``plan.md`` §5.1「③ 结果（第二个事务）」；``docs/architecture.md`` §5
    「各开独立的短会话」）。``retried`` 的决定不落库。

    数据库异常按 ``SQLAlchemyError`` 捕获并吞掉（``plan.md`` §5.1「各自吞异常保循环」）：DB 错误
    原文可能回显行值（``CHECK`` 违例的 ``DETAIL``），不能让它走到监督循环的 ``logger.exception``
    （``AGENTS.md`` §8 第 4 条）。此时返回全零计数——本批决定一行都没提交，而这些行已被认领
    （``attempt_count`` 已递增），下一轮重新认领；真·第 4 次尝试的行由收割支推进终态（``§2.4``）。
    """
    try:
        async with session_factory() as session:
            for delivery in deliveries:
                await _write_one(session, delivery, now=now)
            await session.commit()
    except SQLAlchemyError as exc:
        logger.warning(
            "send_email_result_failed",
            exc_type=type(exc).__name__,
            claimed=len(deliveries),
        )
        return SendEmailResult()
    counts = Counter(delivery.outcome for delivery in deliveries)
    return SendEmailResult(
        sent=counts[_Outcome.SENT],
        failed=counts[_Outcome.FAILED],
        harvested=counts[_Outcome.HARVESTED],
        retried=counts[_Outcome.RETRIED],
    )


async def _write_one(session: AsyncSession, delivery: _Delivery, *, now: datetime) -> None:
    """一行的结果写入：条件 UPDATE 只允许 ``design.md`` §2.3 声明的起点。

    起点条件与 ``rowcount = 0`` 的幂等语义都在仓储层（``mark_sent`` / ``mark_send_failed``），
    本层不再重复判定。写入只碰 ``status`` / ``last_error_class`` / ``last_smtp_code`` / ``sent_at``。
    """
    match delivery.outcome:
        case _Outcome.SENT:
            await mark_sent(session, delivery.message_id, now=now)
        case _Outcome.FAILED:
            # 分类与返回码来自适配器的受控判定：5xx → permanent + 返回码；连接 / 超时 → transient
            # + 无返回码（design.md §2.4「只记 SMTP 返回码与永久 / 临时分类」）。
            await mark_send_failed(
                session,
                delivery.message_id,
                # 失败决定必经 _decide 的分类分支（accepted=False 一定有分类），这里的 cast 只把
                # 「可选」收窄成「必有」，不是兜底；取值仍是 constants 的 ErrorClass 词表。
                error_class=cast("ErrorClass", delivery.error_class),
                smtp_code=delivery.smtp_code,
                now=now,
            )
        case _Outcome.HARVESTED:
            # 收割支：第 4 次尝试的结果事务从未提交，对端到底怎么答的**已不可知**——分区不能凭猜。
            # 按 §2.4 的三类分工记 internal（服务自身内部故障：进程在认领与结果之间中断），
            # 不写成 permanent / transient（那是对端的结论）；本次没有 SMTP 交互，返回码留空。
            await mark_send_failed(
                session,
                delivery.message_id,
                error_class=_INTERNAL,
                smtp_code=None,
                now=now,
            )
        case _Outcome.RETRIED:
            # 不落库：退避已在认领时写好，这一行留待下一轮（design.md §3.3）。
            return


def _build_sender(settings: Settings) -> SmtplibSender:
    """按配置装配发信适配器（``providers/smtp/base.py`` 的接口实现之一）。

    配置一律经 ``config.get_settings()`` 读好再注入（``design.md`` §4.5：配置唯一入口是
    ``config.py``），本层不读环境变量。``mail_from`` / ``reply_to`` 由适配器显式落进报文头，流层
    没有任何入口能改写它们（``AGENTS.md`` §8 第 3 条；``AC-30`` 要求日志与错误里都不出现身份值）。
    """
    return SmtplibSender(
        SmtpConfig(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            tls_mode=settings.smtp_tls_mode,
            ssl_context=(build_tls_context(settings) if settings.smtp_tls_mode == "ssl" else None),
            timeout_seconds=settings.smtp_timeout_seconds,
            mail_from=settings.mail_from_address,
            reply_to=settings.reply_to_address,
        )
    )
