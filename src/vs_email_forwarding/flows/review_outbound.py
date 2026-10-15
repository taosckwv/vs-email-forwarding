#!/usr/bin/env python3
"""流① 审查出站：认领 → 审查 → 落库（`specs/0001-email-forwarding-service` T-23）。

设计真源：``design.md`` §3.2（流① 的状态落点表）、§3 的阶段① ~ ③ 与结果口径、§2.4（重试
与错误列）、§4.2（审查接口）；认领与收割支口径见 ``plan.md`` §5.2、§5.3。``AC-6`` 的集成
落点在阶段 5 的 ``tests/integration/test_review_outbound_flow.py``（T-34）——按 ``plan.md``
§7.1 的分工，流的推进与幂等属集成用例，本模块不带单测。

一次 tick 恒为三段，各开一个短会话，事务不跨外部调用（``docs/architecture.md`` §5）：

* **阶段① 认领**（短事务）：``db/repositories/pending.py`` 用 ``FOR UPDATE SKIP LOCKED`` 取
  一批到点的出站待审行；``attempt_count`` 与 ``next_attempt_at`` 在这一步写定，**提交之后**
  才发起调用。仓储逐行给出收割标志（``plan.md`` §5.3）：``is_harvest`` 为真的是「第 4 次
  尝试已认领、结果事务未提交」的崩溃遗留行。
* **阶段② 调用**（事务外）：逐行经 ``providers/review/factory.py`` 取审查器调用一次；收割行
  **跳过此步**，直接置终态。单次调用受 ``REVIEW_TIMEOUT_SECONDS`` 约束（``design.md`` §4.2：
  超时由调用方施加）。
* **阶段③ 结果**（第二个短事务）：过审 → ``messages`` 一行 ``sending``（即待发行）；判定拒 →
  ``rejected_messages`` 且 ``source = review_rejected``；出错且用尽重试 → ``source =
  review_error`` + ``last_error_class = internal``；收割 → ``source = review_incomplete``。
  三条路径都是同一事务内「写目标行 + 删待审行」（``design.md`` §3.2），结果事务**不**写
  ``attempt_count`` 与 ``next_attempt_at``。未用尽的出错行不落库——退避已在认领时写好，
  留待下一轮。

【例外口径】审查调用是本层唯一「异常类型不受本层约束」的外部能力调用：
``providers/review/base.py`` 把「抛异常 → 审查环节出错（含超时）、用尽后写
``rejected_messages.source = review_error``」定为接口契约的一部分，却不定义任何异常类型
（对照两条先例——发信适配器把失败**全部收进返回值**、不抛异常；IMAP 适配器捕的是 imaplib /
imapclient 的可枚举类型）。阶段② 的捕获面因此只能是 ``Exception``，口径按
``docs/code-style.md`` §2.1 已登记的「例外二：流层的单次外部能力调用边界」执行：
① 只包住那一次审查调用；② 按 ``exc_type`` 记事件、**不留栈**——``logger.exception`` 会把
异常原文写进日志，而审查异常原文可能回显正文或地址（``AGENTS.md`` §8 第 4 条）；③ 不吞取消
（``asyncio.CancelledError`` 继承 ``BaseException``，不在该捕获面内）。阶段③ 的数据库异常按
``SQLAlchemyError`` 这一具体类型捕获：DB 错误原文可能回显行值（如 ``CHECK`` 违例的
``DETAIL``），同样不能进监督循环的 ``logger.exception``。
"""

from __future__ import annotations

import asyncio
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

from vs_email_forwarding.config import Settings
from vs_email_forwarding.constants import (
    DIRECTION_OUTBOUND,
    ERROR_CLASS_INTERNAL,
    MAX_ATTEMPTS,
    REJECT_SOURCE_REVIEW_ERROR,
    REJECT_SOURCE_REVIEW_REJECTED,
    BodyFormat,
)
from vs_email_forwarding.db.repositories.pending import (
    approve_pending_message,
    claim_due_pending_messages,
    harvest_pending_message,
    reject_pending_message,
)

# 会话工厂的类型别名与流⑤ 共用一个定义（`flows/cleanup.py`），不在这里再造一个同名影子类型。
from vs_email_forwarding.flows.cleanup import SessionFactory
from vs_email_forwarding.providers.review import factory as review_factory
from vs_email_forwarding.providers.review.base import Reviewer, ReviewMessage

__all__ = ["ReviewOutboundResult", "review_outbound_tick"]

logger = structlog.get_logger(__name__)

# 受控取值一律从 constants 取具名常量，不在本模块重抄一遍（AGENTS.md §7）：本流只看出站方向；
# 三个拒审来源依次是「判定拒 / 环节出错 / 收割」（design.md §2.1 的 source 列取值）；
# 审查环节出错的分类是「服务自身内部故障」（design.md §2.4）。
_OUTBOUND = DIRECTION_OUTBOUND
_REVIEW_REJECTED = REJECT_SOURCE_REVIEW_REJECTED
_REVIEW_ERROR = REJECT_SOURCE_REVIEW_ERROR
_INTERNAL = ERROR_CLASS_INTERNAL


class _Outcome(StrEnum):
    """一行待审邮件在本次 tick 里的处置（内部词汇，不落库）。"""

    #: 审查通过 → ``messages`` 一行 ``sending``。
    APPROVED = "approved"
    #: 审查判定不通过 → ``rejected_messages.source = review_rejected``。
    REJECTED = "rejected"
    #: 审查环节出错且重试已用尽 → ``source = review_error``。
    ERRORED = "errored"
    #: 崩溃遗留的第 4 次尝试 → ``source = review_incomplete``。
    HARVESTED = "harvested"
    #: 审查环节出错但仍有重试额度 → 不落库，退避已在认领时写好。
    RETRIED = "retried"


@dataclass(frozen=True, slots=True)
class ReviewOutboundResult:
    """一次 tick 的处置计数（只统计**已提交**的结果；进程内使用，不落库）。

    五个字段与 :class:`_Outcome` 一一对应。阶段③ 提交失败时全部为 0——那一批决定一行都没
    提交，待写条数记在 ``review_outbound_result_failed`` 事件里。
    """

    approved: int = 0
    rejected: int = 0
    errored: int = 0
    harvested: int = 0
    retried: int = 0


@dataclass(frozen=True, slots=True)
class _ClaimedReview:
    """阶段① 收成的一行：仓储行转成中性对象，ORM 行不出 ``db/`` 的使用范围。"""

    pending_id: UUID
    attempt_count: int
    is_harvest: bool
    message: ReviewMessage


@dataclass(frozen=True, slots=True)
class _Decision:
    """阶段② 的产物：一行待审邮件的处置决定，阶段③ 据此落库。"""

    pending_id: UUID
    outcome: _Outcome


async def review_outbound_tick(
    session_factory: SessionFactory,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> ReviewOutboundResult:
    """执行一次流① tick：认领一批出站待审邮件、逐行审查、把结果落库。

    ``now`` 供测试注入（``plan.md`` §7.2：时间推进靠把 ``next_attempt_at`` 设成已到期，不靠
    sleep）；缺省取一次带时区的当前 UTC 时间，**本次 tick 的三个阶段共用这一个时刻**，
    退避的起算点不会在阶段之间漂移。

    认领阶段的数据库异常不在这里处理，交给监督循环（``docs/code-style.md`` §2.1 例外一）：
    这条路的错误文本只有方向、时刻与批量，不含邮件内容，留栈可查且不触日志红线。其余环节的
    异常都收进返回值：审查环节的异常按固定重试语义翻成处置（``design.md`` §4.2），结果事务
    的数据库异常只记事件并返回零计数——单条流不会因一次 tick 打死自己（``plan.md`` §5.1）。
    """
    tick_now = now if now is not None else datetime.now(UTC)
    claimed = await _claim(session_factory, now=tick_now, batch_size=settings.batch_size)
    if not claimed:
        return ReviewOutboundResult()
    decisions = await _decide_all(claimed, timeout_seconds=settings.review_timeout_seconds)
    result = await _write_decisions(session_factory, decisions, now=tick_now)
    logger.info(
        "review_outbound_tick",
        approved=result.approved,
        rejected=result.rejected,
        errored=result.errored,
        harvested=result.harvested,
        retried=result.retried,
    )
    return result


async def _claim(
    session_factory: SessionFactory, *, now: datetime, batch_size: int
) -> list[_ClaimedReview]:
    """阶段① 认领（短事务）：取一批到点的出站待审行，提交后才发起调用。

    转换在会话作用域内完成（``design.md`` §8：只消费仓储返回的中性对象与自身需要的字段，
    不把 ORM 行传到别的层或别的 tick）。认领事务必须在发起外部调用之前提交
    （``plan.md`` §5.1）：提交即释放行锁，外部调用不在事务内进行。
    """
    async with session_factory() as session:
        claimed = await claim_due_pending_messages(
            session, direction=_OUTBOUND, now=now, batch_size=batch_size
        )
        batch: list[_ClaimedReview] = []
        for row, is_harvest in claimed:
            batch.append(
                _ClaimedReview(
                    pending_id=row.id,
                    # 认领后的 attempt_count 就是本次尝试的序号（1 ~ MAX_ATTEMPTS）：
                    # 阶段② 出错时用它判断还有没有重试额度。
                    attempt_count=row.attempt_count,
                    is_harvest=is_harvest,
                    message=ReviewMessage(
                        # §4.2 的 message_id 是「该邮件的对外 ID（即主键）」，不是 Message-ID
                        # 头（那是 pending_messages.message_id 列，本流用不到）。
                        message_id=row.id,
                        direction=_OUTBOUND,
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


async def _decide_all(
    claimed: Sequence[_ClaimedReview], *, timeout_seconds: int
) -> list[_Decision]:
    """阶段② 调用（事务外）：逐行得出处置决定；收割行不调用审查器。

    审查器**在运行期**经 ``providers/review/factory.py`` 取用：工厂是阶段 5 集成夹具
    ``fake_review`` 的替换点（``plan.md`` §7.2），在 import 期绑定它的返回值会让替换失效。
    """
    reviewer = review_factory.create_reviewer()
    decisions: list[_Decision] = []
    for claim in claimed:
        if claim.is_harvest:
            # 收割支：不递增、不推退避、不发起任何外部调用（design.md §3 阶段① / plan.md §5.3）。
            decisions.append(_Decision(pending_id=claim.pending_id, outcome=_Outcome.HARVESTED))
            continue
        decisions.append(await _decide(reviewer, claim, timeout_seconds=timeout_seconds))
    return decisions


async def _decide(reviewer: Reviewer, claim: _ClaimedReview, *, timeout_seconds: int) -> _Decision:
    """审一行并翻成处置决定：``design.md`` §4.2 的两条路径 + 出错后的重试算术。

    超时由本层施加（接口不读配置，``providers/review/base.py``）：``asyncio.timeout`` 到点抛
    ``TimeoutError``，与审查器自己抛异常走同一条「出错」路径（``design.md`` §4.2 的「异常 /
    超时」一行）。
    """
    try:
        async with asyncio.timeout(timeout_seconds):
            decision = await reviewer.review(claim.message)
    except Exception as exc:  # 口径见模块 docstring 的【例外口径】与 code-style.md §2.1 例外二
        # 捕获面只能是 Exception：审查器的异常类型不受本层约束，收窄会让非预期异常静默偏离
        # design.md §3.2 的 review_error 语义。只记类型与消息 ID，不留栈（异常原文可能回显
        # 正文或地址，AGENTS.md §8 第 4 条）；CancelledError 继承 BaseException，不在本面内。
        exhausted = claim.attempt_count >= MAX_ATTEMPTS
        logger.warning(
            "review_outbound_review_failed",
            message_id=str(claim.pending_id),
            attempt_count=claim.attempt_count,
            exc_type=type(exc).__name__,
            will_retry=not exhausted,
        )
        outcome = _Outcome.ERRORED if exhausted else _Outcome.RETRIED
        return _Decision(pending_id=claim.pending_id, outcome=outcome)
    # ``"pass"`` 是接口自己给的受控词表（providers/review/base.py 的 ReviewDecision，
    # design.md §4.2）：mypy 按该 Literal 校验这里的比较，写错即报错，不另立第二份取值来源。
    if decision == "pass":
        return _Decision(pending_id=claim.pending_id, outcome=_Outcome.APPROVED)
    return _Decision(pending_id=claim.pending_id, outcome=_Outcome.REJECTED)


async def _write_decisions(
    session_factory: SessionFactory, decisions: Sequence[_Decision], *, now: datetime
) -> ReviewOutboundResult:
    """阶段③ 结果（第二个短事务）：本批决定逐行落库，最后统一提交。

    一批一个短会话（``plan.md`` §5.1「③ 结果（第二个事务）」；``docs/architecture.md`` §5
    「各开独立的短会话」）。未用尽的出错行不落库——退避已在认领时写好，留待下一轮认领。

    数据库异常按 ``SQLAlchemyError`` 捕获并吞掉（``plan.md`` §5.1「各自吞异常保循环」）：
    DB 错误原文可能回显行值（``CHECK`` 违例的 ``DETAIL``），不能让它走到监督循环的
    ``logger.exception``（``AGENTS.md`` §8 第 4 条）。此时返回全零计数——本批决定一行都没提交，
    而这些行已被认领（``attempt_count`` 已递增），下一轮重新认领；真·第 4 次尝试的行由收割支
    推进终态（``design.md`` §3 阶段①）。
    """
    try:
        async with session_factory() as session:
            for decision in decisions:
                await _write_one(session, decision, now=now)
            await session.commit()
    except SQLAlchemyError as exc:
        logger.warning(
            "review_outbound_result_failed",
            exc_type=type(exc).__name__,
            claimed=len(decisions),
        )
        return ReviewOutboundResult()
    counts = Counter(decision.outcome for decision in decisions)
    return ReviewOutboundResult(
        approved=counts[_Outcome.APPROVED],
        rejected=counts[_Outcome.REJECTED],
        errored=counts[_Outcome.ERRORED],
        harvested=counts[_Outcome.HARVESTED],
        retried=counts[_Outcome.RETRIED],
    )


async def _write_one(session: AsyncSession, decision: _Decision, *, now: datetime) -> None:
    """一行的结果写入：写目标行 + 删待审行都在本会话内，由调用方统一提交。

    撞唯一键 / 主键时仓储按「这一封已经处理过」处理（只删待审行，``design.md`` §3.2），
    本层无须再去重。
    """
    match decision.outcome:
        case _Outcome.APPROVED:
            await approve_pending_message(session, decision.pending_id, now=now)
        case _Outcome.REJECTED:
            await reject_pending_message(
                session, decision.pending_id, source=_REVIEW_REJECTED, now=now
            )
        case _Outcome.ERRORED:
            # 出错用尽重试：分类是「服务自身内部故障」，不是对端的拒绝（design.md §2.4）。
            await reject_pending_message(
                session,
                decision.pending_id,
                source=_REVIEW_ERROR,
                error_class=_INTERNAL,
                now=now,
            )
        case _Outcome.HARVESTED:
            await harvest_pending_message(session, decision.pending_id, now=now)
        case _Outcome.RETRIED:
            # 不落库：退避已在认领时写好，这一行留待下一轮（design.md §3 的结果口径）。
            return
