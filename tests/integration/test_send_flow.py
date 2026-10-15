#!/usr/bin/env python3
"""流② 发信：集成用例（`T-35`）。

覆盖 `tasks.md` T-35 的 AC 清单：`AC-4` / `AC-5` / `AC-7` / `AC-8` / `AC-9` / `AC-19` /
`AC-21` / `AC-24` / `AC-29` / `AC-30` / `AC-34` / `AC-37` / `AC-40`。用例名沿用 `plan.md`
§7.3 为每条 `AC` 指定的落点，测试手法沿用 §7.4（`AC-21` / `AC-37` / `AC-40` 三条最容易被
写成「看起来过了、其实没测到」，逐条照 §7.4 的写法）。

**真依赖与替身边界**（`docs/testing.md` §4.1 / §4.2）：数据库是本机真实 PostgreSQL 的运行级
隔离库（`conftest.py` 建库 + 迁移 + 清表）；SMTP 是进程内假服务器，但**投递仍由生产适配器
`SmtplibSender` 经标准库 `smtplib` 走 TCP**，因此发件人身份（`AC-4` / `AC-5`）与返回码分类
（`AC-8` / `AC-30` / `AC-34`）都是在线上字节上判定的，不是从被测实现的对象里读出来的。

**时间口径**（`docs/testing.md` R12）：除 `AC-21`（§7.4 明说「唯一允许真实等待」的那条）与
`AC-40` 的耗时上界之外，一律把时刻注入 `send_email_tick(now=...)`、靠把 `next_attempt_at`
置成已到点来推进，不 sleep、不依赖真实时钟。

**期望值出处**（`docs/testing.md` R2）：退避 15 / 30 / 60 秒取自 `AC-9` 与 `AGENTS.md` §8.7 的
明文；SMTP 返回码取自本文件自己脚本化的假服务器回复（`smtp_capture.reply_text`），不是从
被测实现读回的；状态与分类判据取自 `design.md` §2.3 的状态机与 §3.3 的处置表。
"""

from __future__ import annotations

import asyncio
import contextlib
import email
import email.policy
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import conftest
from conftest import FakeReviewer, FakeSmtpServer
from vs_email_forwarding.config import Settings
from vs_email_forwarding.constants import (
    BODY_FORMAT_TEXT,
    DIRECTION_OUTBOUND,
    ERROR_CLASS_INTERNAL,
    ERROR_CLASS_PERMANENT,
    ERROR_CLASS_TRANSIENT,
    MAX_ATTEMPTS,
    MESSAGE_STATE_FAILED,
    MESSAGE_STATE_SENDING,
    MESSAGE_STATE_SENT,
)
from vs_email_forwarding.db.models import Base, Message
from vs_email_forwarding.flows.review_outbound import review_outbound_tick
from vs_email_forwarding.flows.send_email import SendEmailResult, send_email_tick
from vs_email_forwarding.main import create_app
from vs_email_forwarding.providers.smtp.base import OutboundMessage

pytestmark = pytest.mark.integration

#: 测试注入的时刻（`plan.md` §7.2：时间推进靠注入，不靠 sleep）。
_NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)

#: 造态行的「提交时刻」：只用来让 `created_at` / `reviewed_at` 与 tick 时刻可区分
#: （`design.md` §3.3 的结果事务**不得**改写它们，`AC-9` 的差值断言因此不会与它们混淆）。
_SUBMITTED_AT = datetime(2025, 5, 31, 8, 30, 0, tzinfo=UTC)

#: 占位地址与内容一律用 `example.com` / `example.net`，正文不带真实信息（`AGENTS.md` §8.4）。
_RECIPIENT = "someone@example.net"
_SUBJECT = "integration-subject"
_BODY = "integration-body"

#: 本文件自己脚本化的对端回复：期望的返回码就是这两个字面量（`AC-8` / `AC-34` / `AC-30`）。
_TRANSIENT_REPLY = "451 4.3.0 try later"
_PERMANENT_REPLY = "550 5.1.1 user unknown"

#: `AC-9` / `AGENTS.md` §8.7 的退避明文（不取自 `constants`，避免与被测实现同源）。
_BACKOFF_SECONDS = (15.0, 30.0, 60.0)

#: `AC-29` 的认领闸门超时：另一方被行锁挡住时不必把用例挂死（见 `_ClaimWindowFactory`）。
_GATE_TIMEOUT_SECONDS = 2.0

#: 与 `AC-40` 的 `SMTP_TIMEOUT_SECONDS` 对应的等待上限容差（§7.4：断言「有上界」而非精确值）。
_TIMEOUT_TOLERANCE_SECONDS = 3.0

SessionFactory = async_sessionmaker[AsyncSession]


async def _read_row(factory: SessionFactory, message_id: UUID) -> dict[str, Any]:
    """读回该出站行的判定面列（`design.md` §2.1 的列语义，不读实现内部状态）。"""
    async with factory() as session:
        row = (
            await session.execute(sa.select(Message).where(Message.id == message_id))
        ).scalar_one()
        return {
            "status": row.status,
            "attempt_count": row.attempt_count,
            "next_attempt_at": row.next_attempt_at,
            "sent_at": row.sent_at,
            "last_error_class": row.last_error_class,
            "last_smtp_code": row.last_smtp_code,
        }


async def _seed_sending(
    factory: SessionFactory,
    *,
    attempt_count: int = 0,
    next_attempt_at: datetime = _NOW,
    recipient: str = _RECIPIENT,
    subject: str = _SUBJECT,
    body: str = _BODY,
) -> UUID:
    """造一行「已过审、待发」的出站行（`design.md` §2.3 的 `sending`）。

    这是流① 过审后写下的形态（那条路径的判定面归 `T-34`）。直接造态让本文件只判流② 的行为，
    不依赖别的流先跑（`plan.md` §8.1 的「单条流各自可验」）。
    """
    message_id = uuid4()
    async with factory() as session:
        session.add(
            Message(
                id=message_id,
                direction=DIRECTION_OUTBOUND,
                status=MESSAGE_STATE_SENDING,
                message_id=f"<{uuid4()}@example.com>",
                recipient=recipient,
                subject=subject,
                body=body,
                body_format=BODY_FORMAT_TEXT,
                attempt_count=attempt_count,
                next_attempt_at=next_attempt_at,
                created_at=_SUBMITTED_AT,
                reviewed_at=_SUBMITTED_AT,
            )
        )
        await session.commit()
    return message_id


async def _submit_and_approve(
    client: TestClient, factory: SessionFactory, settings: Settings
) -> tuple[UUID, datetime]:
    """经 `POST /v1/messages` 提交，再跑一次流① 让它进入 `sending`（`AC-7` 的入口段）。

    提交端点不注入时刻（`service/submit_message` 自己取真实 UTC），因此返回的 tick 时刻取在
    提交**之后**：认领谓词要的「已到点」由真实先后关系保证，不靠把时钟往回拨。
    """
    response = client.post(
        "/v1/messages",
        json={
            "recipient": _RECIPIENT,
            "subject": _SUBJECT,
            "body": _BODY,
            "body_format": BODY_FORMAT_TEXT,
        },
    )
    assert response.status_code == 200, response.text
    message_id = UUID(response.json()["id"])
    tick_now = datetime.now(UTC)
    reviewed = await review_outbound_tick(factory, settings, now=tick_now)
    assert reviewed.approved == 1, "造态失败：流① 没把这封过审行推进到 sending"
    return message_id, tick_now


async def _attempt(
    factory: SessionFactory,
    settings: Settings,
    message_id: UUID,
    *,
    now: datetime,
) -> tuple[SendEmailResult, dict[str, Any]]:
    """跑一次 tick 并读回该行快照：认领时刻由调用方注入，不 sleep、不依赖真实时间。"""
    result = await send_email_tick(factory, settings, now=now)
    return result, await _read_row(factory, message_id)


async def _persisted_text(factory: SessionFactory) -> str:
    """把库中**所有表所有列**的值拼成一段文本（`AC-30` 的全表否定断言扫描面）。"""
    chunks: list[str] = []
    async with factory() as session:
        for table in Base.metadata.sorted_tables:
            for row in (await session.execute(sa.select(table))).mappings().all():
                for value in row.values():
                    if value is None:
                        continue
                    if isinstance(value, bytes):
                        chunks.append(value.decode("utf-8", "replace"))
                    else:
                        chunks.append(str(value))
    return "\n".join(chunks)


def _decoded_body(raw: bytes) -> str:
    """解出实收报文的正文（按声明的内容编码解码，判定「正文真的到达」）。

    MIME 部件末尾那一个 CRLF 是编码与分界的产物、不属于正文，两侧都去尾后再比较。
    """
    message = email.message_from_bytes(raw, policy=email.policy.default)
    return str(message.get_content()).rstrip("\r\n")


class _ClaimWindowSession:
    """会话代理：只在**认领查询之后、提交之前**插一道闸门，其余调用原样转发。

    除 `scalars`（认领 SELECT 的入口）、`execute`（结果 UPDATE 的入口）与 `commit` 之外一律
    经 `__getattr__` 转发，因此被测实现看到的调用面与真实会话一致。
    """

    def __init__(self, inner: AsyncSession, factory: _ClaimWindowFactory) -> None:
        self.__dict__["_inner"] = inner
        self.__dict__["_factory"] = factory

    async def scalars(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        """执行认领 SELECT，然后等在闸门上（事务仍未提交）。"""
        result = await self._inner.scalars(statement, *args, **kwargs)
        await self._factory.pass_gate()
        return result

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._inner.execute(statement, *args, **kwargs)

    async def commit(self) -> None:
        await self._inner.commit()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["_inner"], name)


class _ClaimWindowFactory:
    """把两次并发 tick 的「认领 SELECT 已执行、事务尚未提交」这个窗口撑开。

    `AC-29` 的判定对象是「同一行只被一个执行体认领」，而这件事只有在两个认领事务**真的重叠**
    时才可观测：不重叠时，认领事务写回的 `next_attempt_at` 自己就挡住了第二次认领——用例照样
    绿，却什么也没验证（这正是 `plan.md` §7.4 说的「看起来过了、其实没测到」）。撑窗口的办法
    是在会话的认领查询处设一道双向闸门：两个执行体都执行完各自的认领 SELECT 之后才允许继续。

    闸门带超时：若另一方的 SELECT 被行锁挡住（`FOR UPDATE` 不加 `SKIP LOCKED` 的情形），
    超时即放行——「被挡住」本身就是锁生效的表现，不必把用例挂死。放进 `tests/` 的这层替身
    只替代**调度时序**，不替代数据库，也不替被测实现做判定。
    """

    def __init__(self, inner: SessionFactory, *, parties: int) -> None:
        self._inner = inner
        self._parties = parties
        self._arrived = 0
        self._open = asyncio.Event()

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[AsyncSession]:
        async with self._inner() as session:
            yield cast("AsyncSession", _ClaimWindowSession(session, self))

    async def pass_gate(self) -> None:
        """认领 SELECT 已完成的一方在此等另一方；超时即放行（见类 docstring）。"""
        self._arrived += 1
        if self._arrived >= self._parties:
            self._open.set()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._open.wait(), timeout=_GATE_TIMEOUT_SECONDS)


class TestDeliveredOnce:
    """`AC-7`：真实 PostgreSQL + 假 SMTP 下跑完流① 与流②，捕获端收到且库中为已发出。"""

    async def test_outbound_end_to_end(
        self,
        client: TestClient,
        session_factory: SessionFactory,
        integration_environment: Settings,
        fake_review: FakeReviewer,
        smtp_capture: FakeSmtpServer,
    ) -> None:
        """`AC-7`：跑完流① 与流② 各一次 tick，投递被捕获，且状态流转到 `sent`。"""
        message_id, tick_now = await _submit_and_approve(
            client, session_factory, integration_environment
        )

        # 提交 + 过审只应到「投递中」：此时还没碰 SMTP（把两段分开断言，避免一条断言同时兜两件事）。
        before = await _read_row(session_factory, message_id)
        assert before["status"] == MESSAGE_STATE_SENDING
        assert smtp_capture.delivery_count == 0

        result = await send_email_tick(session_factory, integration_environment, now=tick_now)

        assert result.sent == 1
        assert smtp_capture.delivery_count == 1
        delivery = smtp_capture.deliveries[0]
        assert delivery.recipients == (_RECIPIENT,)
        assert delivery.header("Subject") == _SUBJECT
        assert _decoded_body(delivery.raw) == _BODY
        after = await _read_row(session_factory, message_id)
        assert after["status"] == MESSAGE_STATE_SENT
        assert after["sent_at"] == tick_now
        assert after["attempt_count"] == 1
        assert after["last_error_class"] is None
        assert after["last_smtp_code"] is None


class TestSenderIdentity:
    """`AC-4` / `AC-5`：发件人身份只来自服务配置，`Reply-To` 恒等于拉取信箱。"""

    async def test_from_fixed_from_config(
        self,
        client: TestClient,
        session_factory: SessionFactory,
        settings_override: Callable[..., Settings],
        fake_review: FakeReviewer,
        smtp_capture: FakeSmtpServer,
    ) -> None:
        """`AC-4`：请求体多带发件人字段（未知字段）也发得出去，`From` 仍是配置值。"""
        settings = settings_override(
            MAIL_FROM_ADDRESS="forwarder@example.com",
            REPLY_TO_ADDRESS="inbox@example.com",
            IMAP_ADDRESS="inbox@example.com",
        )
        caller_address = "attacker@evil.example"
        response = client.post(
            "/v1/messages",
            json={
                "recipient": _RECIPIENT,
                "subject": _SUBJECT,
                "body": _BODY,
                "body_format": BODY_FORMAT_TEXT,
                # 契约 `additionalProperties: true`：同名未知字段忽略、不报错（`schemas/messages.py`）。
                "from": caller_address,
                "from_address": caller_address,
                "sender": caller_address,
                "reply_to": caller_address,
            },
        )
        assert response.status_code == 200, response.text
        # 契约 `MessageId`：响应里的 id 必须能解析成 UUID（不是任意串、也不是空）。
        UUID(response.json()["id"])
        tick_now = datetime.now(UTC)
        assert (await review_outbound_tick(session_factory, settings, now=tick_now)).approved == 1

        assert (await send_email_tick(session_factory, settings, now=tick_now)).sent == 1

        delivery = smtp_capture.deliveries[0]
        # 身份头在线上字节上判定：既等于配置值，也不等于调用方给的那个地址。
        assert delivery.header("From") == settings.mail_from_address
        assert caller_address not in (delivery.header("From") or "")
        assert delivery.mail_from == settings.mail_from_address
        assert _decoded_body(delivery.raw) == _BODY
        # 调用方连「指定发件人」的入口都不存在：接口形状里就没有身份字段（`design.md` §4.3）。
        assert "from" not in {field.name for field in fields(OutboundMessage)}

    async def test_reply_to_written(
        self,
        session_factory: SessionFactory,
        settings_override: Callable[..., Settings],
        smtp_capture: FakeSmtpServer,
    ) -> None:
        """`AC-5`：实收报文含 `Reply-To`，其值等于专用回复邮箱配置项（= IMAP 信箱）。"""
        settings = settings_override(
            MAIL_FROM_ADDRESS="forwarder@example.com",
            REPLY_TO_ADDRESS="inbox@example.com",
            IMAP_ADDRESS="inbox@example.com",
        )
        await _seed_sending(session_factory)

        assert (await send_email_tick(session_factory, settings, now=_NOW)).sent == 1

        delivery = smtp_capture.deliveries[0]
        assert delivery.header("Reply-To") == settings.reply_to_address
        # 两条独立判据：既等于配置项，也等于拉取信箱（`AGENTS.md` §8 第 3 条），
        # 且与 `From` 是不同的地址——否则「显式写 Reply-To」会被 `From` 恰好相等掩盖。
        assert settings.reply_to_address == settings.imap_address
        assert settings.reply_to_address != settings.mail_from_address


class TestRetryArithmetic:
    """`AC-9` / `AC-8` / `AC-34`：退避表、重试用尽后终态、`5xx` 直接终态。"""

    async def test_backoff_written_on_claim(
        self,
        session_factory: SessionFactory,
        integration_environment: Settings,
        smtp_capture: FakeSmtpServer,
    ) -> None:
        """`AC-9`：三次重试的间隔分别为 15 / 30 / 60 秒（每次与认领时刻作差）。"""
        smtp_capture.reply_text = _TRANSIENT_REPLY
        message_id = await _seed_sending(session_factory)

        now = _NOW
        delays: list[float] = []
        for expected_attempt in (1, 2, 3):
            result, row = await _attempt(
                session_factory, integration_environment, message_id, now=now
            )
            assert result.retried == 1
            assert row["attempt_count"] == expected_attempt
            # 未达上限：状态保持 sending 等下一轮认领，退避已由认领事务写定。
            assert row["status"] == MESSAGE_STATE_SENDING
            delays.append((row["next_attempt_at"] - now).total_seconds())
            now = row["next_attempt_at"]

        assert delays == list(_BACKOFF_SECONDS)
        # 三次都是**真投递**（不是空转）：捕获端确实收到三次尝试。
        assert smtp_capture.delivery_count == 3
        # 中途失败不写错误列（`design.md` §2.4：该分支只在终态落分类）。
        assert row["last_error_class"] is None
        assert row["last_smtp_code"] is None

    async def test_transient_exhausts_attempts(
        self,
        session_factory: SessionFactory,
        integration_environment: Settings,
        smtp_capture: FakeSmtpServer,
    ) -> None:
        """`AC-8`：临时性错误连续报错时，第 4 次尝试失败后进终态并记最后一次返回码与分类。"""
        smtp_capture.reply_text = _TRANSIENT_REPLY
        message_id = await _seed_sending(session_factory)

        now = _NOW
        row: dict[str, Any] = {}
        for _ in range(MAX_ATTEMPTS - 1):
            result, row = await _attempt(
                session_factory, integration_environment, message_id, now=now
            )
            assert result.retried == 1
            now = row["next_attempt_at"]

        result, row = await _attempt(session_factory, integration_environment, message_id, now=now)

        assert result.failed == 1
        assert row["attempt_count"] == MAX_ATTEMPTS
        assert row["status"] == MESSAGE_STATE_FAILED
        assert row["last_error_class"] == ERROR_CLASS_TRANSIENT
        assert row["last_smtp_code"] == 451
        assert smtp_capture.delivery_count == MAX_ATTEMPTS

        # 终态之后不再产生尝试：把时刻推到很久以后也只该是零计数。
        later = await send_email_tick(
            session_factory, integration_environment, now=now + timedelta(hours=1)
        )
        assert (later.sent, later.failed, later.harvested, later.retried) == (0, 0, 0, 0)
        assert smtp_capture.delivery_count == MAX_ATTEMPTS

    async def test_permanent_fails_immediately(
        self,
        session_factory: SessionFactory,
        integration_environment: Settings,
        smtp_capture: FakeSmtpServer,
    ) -> None:
        """`AC-34`：持续 `5xx` 时第一次尝试后即终态，`attempt_count == 1`，分类记为永久。"""
        smtp_capture.reply_text = _PERMANENT_REPLY
        message_id = await _seed_sending(session_factory)

        result, row = await _attempt(session_factory, integration_environment, message_id, now=_NOW)

        assert result.failed == 1
        assert row["attempt_count"] == 1
        assert row["status"] == MESSAGE_STATE_FAILED
        assert row["last_error_class"] == ERROR_CLASS_PERMANENT
        assert row["last_smtp_code"] == 550
        # 「不出现退避等待」的判据：终态在**同一次 tick** 内到达，且之后再无尝试。
        # （`next_attempt_at` 一列由认领事务写定、结果事务一律不碰——`design.md` §2.4 / §3.3；
        # 行已终态，那一列不再参与认领，因此这里判行为而不判该列未被推后。）
        later = await send_email_tick(
            session_factory, integration_environment, now=_NOW + timedelta(hours=1)
        )
        assert (later.sent, later.failed, later.harvested, later.retried) == (0, 0, 0, 0)
        assert smtp_capture.delivery_count == 1


class TestHarvestBranch:
    """`AC-37`：崩溃遗留的第 4 次尝试被收割，不得永久停在「投递中」，也不产生第 5 次投递。"""

    async def test_reaper_after_fourth_attempt(
        self,
        session_factory: SessionFactory,
        integration_environment: Settings,
        smtp_capture: FakeSmtpServer,
    ) -> None:
        """`AC-37`：按 §7.4 的四步确定性造态，判定对象是收割支而不是进程生命周期。"""
        smtp_capture.reply_text = _TRANSIENT_REPLY
        message_id = await _seed_sending(session_factory)

        # 步骤 1：跑满 3 次尝试，该行到 attempt_count == 3、状态仍为 sending（退避已推到第 4 轮）。
        now = _NOW
        row: dict[str, Any] = {}
        for _ in range(MAX_ATTEMPTS - 1):
            _, row = await _attempt(session_factory, integration_environment, message_id, now=now)
            now = row["next_attempt_at"]
        assert row["attempt_count"] == MAX_ATTEMPTS - 1

        # 步骤 2：构造崩溃窗口的遗留形态——第 4 次尝试已被认领、结果事务未提交。
        async with session_factory() as session:
            await session.execute(
                sa.update(Message)
                .where(Message.id == message_id)
                .values(attempt_count=MAX_ATTEMPTS)
            )
            await session.commit()
        left_over = await _read_row(session_factory, message_id)
        assert left_over["status"] == MESSAGE_STATE_SENDING
        assert left_over["attempt_count"] == MAX_ATTEMPTS
        baseline = smtp_capture.delivery_count

        # 步骤 3：一次 tick 把它推进终态，不得永久停在「投递中」。
        result, harvested = await _attempt(
            session_factory, integration_environment, message_id, now=now
        )

        assert result.harvested == 1
        assert harvested["status"] == MESSAGE_STATE_FAILED
        # 收割支没有对端结论可记：分类是服务自身内部故障（`design.md` §3.3），返回码留空。
        assert harvested["last_error_class"] == ERROR_CLASS_INTERNAL
        assert harvested["last_smtp_code"] is None
        assert harvested["attempt_count"] == MAX_ATTEMPTS
        # 步骤 4：捕获端的投递次数与步骤 2 结束时相同——没有第 5 次投递。
        assert smtp_capture.delivery_count == baseline


class TestExactlyOnce:
    """`AC-29`：同一封出站邮件恰好一次 SMTP 投递（并发抢占与重放两个角度）。"""

    async def test_concurrent_claim_single_delivery(
        self,
        session_factory: SessionFactory,
        integration_environment: Settings,
        smtp_capture: FakeSmtpServer,
    ) -> None:
        """`AC-29`：两个并发执行体同时抢占同一批待发行，捕获端只收到一封。"""
        message_id = await _seed_sending(session_factory)
        aligned = _ClaimWindowFactory(session_factory, parties=2)

        first, second = await asyncio.gather(
            send_email_tick(aligned, integration_environment, now=_NOW),
            send_email_tick(aligned, integration_environment, now=_NOW),
        )

        assert first.sent + second.sent == 1
        assert smtp_capture.delivery_count == 1
        assert (await _read_row(session_factory, message_id))["status"] == MESSAGE_STATE_SENT

    async def test_sent_row_is_not_claimed_again(
        self,
        session_factory: SessionFactory,
        integration_environment: Settings,
        smtp_capture: FakeSmtpServer,
    ) -> None:
        """`AC-29`（重放角度）：已发出的行即使已到点也不再被认领，不产生第二次投递。"""
        message_id = await _seed_sending(session_factory)
        assert (await send_email_tick(session_factory, integration_environment, now=_NOW)).sent == 1

        again = await send_email_tick(
            session_factory, integration_environment, now=_NOW + timedelta(hours=1)
        )

        assert (again.sent, again.failed, again.harvested, again.retried) == (0, 0, 0, 0)
        assert smtp_capture.delivery_count == 1
        assert (await _read_row(session_factory, message_id))["status"] == MESSAGE_STATE_SENT


class TestRefusalRedLines:
    """`AC-30` / `AC-24`：对端原话与邮件内容不得落库、不得进日志（`AGENTS.md` §8 第 4 条）。"""

    async def test_error_has_no_free_text(
        self,
        session_factory: SessionFactory,
        integration_environment: Settings,
        smtp_capture: FakeSmtpServer,
    ) -> None:
        """`AC-30`：对端回复文本含地址时，库中只留返回码与分类，全表不出现那段文本或该地址。"""
        free_text = "mailbox unavailable"
        echoed_address = "bounce-probe@example.com"
        smtp_capture.reply_text = f"550 5.1.1 <{echoed_address}> {free_text}"
        message_id = await _seed_sending(session_factory)

        result, row = await _attempt(session_factory, integration_environment, message_id, now=_NOW)

        assert result.failed == 1
        assert row["last_smtp_code"] == 550
        assert row["last_error_class"] == ERROR_CLASS_PERMANENT
        # 全表扫描（四张表的每一列）：对端原话与它回显的地址一个字都不许留下。
        # 先绑定再断言、且失败信息里不带库内容——否则 pytest 的断言自省会把这四张表的文本
        # dump 进失败报告，等于把「不许留」的东西又抄一遍（`docs/testing.md` §4.7）。
        persisted = await _persisted_text(session_factory)
        leaked = [text for text in (free_text, echoed_address, "5.1.1") if text in persisted]
        assert leaked == [], "库中出现了本不该留下的对端文本（此处不回显库内容）"

    async def test_logs_no_content(
        self,
        session_factory: SessionFactory,
        integration_environment: Settings,
        smtp_capture: FakeSmtpServer,
        capsys: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """`AC-24`：投递一封正文与收件人都可识别的邮件后，日志文本里两者都不出现。"""
        body_marker = "BODYMARKER7F3A9C"
        recipient_marker = "recipient-7f3a9c@example.com"
        smtp_capture.reply_text = _TRANSIENT_REPLY
        await _seed_sending(
            session_factory, recipient=recipient_marker, body=f"body-{body_marker}-end"
        )

        await send_email_tick(session_factory, integration_environment, now=_NOW)

        captured = capsys.readouterr()
        # 采集面 = 两条标准流 + `caplog`：structlog 的末端渲染器由**进程级**配置决定
        # （`logging.configure_logging` 换掉 root handler，单测可能先跑过），只看 `caplog`
        # 会因执行顺序而空转；三处合并后无论哪条链路在渲染，文本都在扫描面里。
        text = "\n".join((captured.out, captured.err, caplog.text))
        # 非空转证明：本次 tick 的事件确实进了这个扫描面（否则下面的否定断言恒绿）。
        assert "send_email_tick" in text
        assert body_marker not in text
        assert recipient_marker not in text


class TestRestartAndTimeBounds:
    """`AC-19` / `AC-21` / `AC-40`：重启恢复、不干预的三分钟上界、挂起不占用执行体。"""

    async def test_restart_resumes(
        self,
        session_factory: SessionFactory,
        integration_environment: Settings,
        test_database: conftest.TestDatabase,
        smtp_capture: FakeSmtpServer,
    ) -> None:
        """`AC-19`：留一行未处理的 `sending`，重新构造应用与引擎后一次 tick 即推进到 `sent`。"""
        # 前一个「进程」留下的待发行：已经历两次失败，退避已到点。
        message_id = await _seed_sending(
            session_factory,
            attempt_count=2,
            next_attempt_at=_NOW - timedelta(seconds=1),
        )
        assert smtp_capture.delivery_count == 0

        # 重启等价物（§7.4：不真杀进程）：新的应用实例 + 新引擎，进程内没有任何状态被带过来。
        restarted_app = create_app()
        # 「重新构造应用」不是装饰：新实例必须真的能服务请求。`include_router` 在本版
        # FastAPI 里是懒装配（`app.routes` 里只有 `_IncludedRouter` 占位），按路径集合判会
        # 落空，因此直接打一次健康检查——它只依赖隔离库，不进入 lifespan。
        assert TestClient(restarted_app).get("/healthz").status_code == 200
        restarted_engine: AsyncEngine = create_async_engine(test_database.url)
        try:
            restarted_factory: SessionFactory = async_sessionmaker(
                bind=restarted_engine, expire_on_commit=False
            )
            result = await send_email_tick(restarted_factory, integration_environment, now=_NOW)
            assert result.sent == 1
        finally:
            await restarted_engine.dispose()

        assert smtp_capture.delivery_count == 1
        resumed = await _read_row(session_factory, message_id)
        assert resumed["status"] == MESSAGE_STATE_SENT
        assert resumed["attempt_count"] == 3

    async def test_within_three_minutes(
        self,
        client: TestClient,
        session_factory: SessionFactory,
        settings_override: Callable[..., Settings],
        fake_review: FakeReviewer,
        smtp_capture: FakeSmtpServer,
    ) -> None:
        """`AC-21`：提交后不干预，驱动循环在 180 秒上界内让它进入已发出（§7.4 唯一真等的一条）。"""
        # 真实的等待被压到配置值：间隔 1 秒 → 每次迭代真等 1 秒，仍然断言「不干预也能走完」。
        settings = settings_override(POLL_SEND_SECONDS=1)
        interval = settings.poll_send_seconds
        submitted_at = datetime.now(UTC)
        deadline = submitted_at + timedelta(seconds=180)
        response = client.post(
            "/v1/messages",
            json={
                "recipient": _RECIPIENT,
                "subject": _SUBJECT,
                "body": _BODY,
                "body_format": BODY_FORMAT_TEXT,
            },
        )
        assert response.status_code == 200, response.text
        message_id = UUID(response.json()["id"])

        # 驱动循环：审查流与发送流各按自己的间隔跑，测试**不碰**库中状态、只观察。
        sent_at: datetime | None = None
        await review_outbound_tick(session_factory, settings, now=datetime.now(UTC))
        while datetime.now(UTC) < deadline:
            await asyncio.sleep(interval)
            result = await send_email_tick(session_factory, settings, now=datetime.now(UTC))
            if result.sent:
                sent_at = (await _read_row(session_factory, message_id))["sent_at"]
                break

        assert sent_at is not None, "提交后未干预，180 秒上界内没走到「已发出」"
        assert submitted_at < sent_at < deadline
        assert smtp_capture.delivery_count == 1

    async def test_timeout_bounded(
        self,
        session_factory: SessionFactory,
        settings_override: Callable[..., Settings],
        smtp_capture: FakeSmtpServer,
    ) -> None:
        """`AC-40`：假服务器接受连接后挂住，单次尝试在超时值内有界收场，随后仍能发出。"""
        settings = settings_override(SMTP_TIMEOUT_SECONDS=1)
        timeout_seconds = settings.smtp_timeout_seconds
        smtp_capture.mode = "hang"
        message_id = await _seed_sending(session_factory)

        started = time.monotonic()
        result, row = await _attempt(session_factory, settings, message_id, now=_NOW)
        elapsed = time.monotonic() - started

        # 断言的是「有上界」（§7.4 明说带容差），不是精确值。
        assert elapsed < timeout_seconds + _TIMEOUT_TOLERANCE_SECONDS
        assert result.retried == 1
        assert row["attempt_count"] == 1
        assert row["status"] == MESSAGE_STATE_SENDING
        # 连接 / 超时类故障没有返回码可记，也不写错误列（未到终态）。
        assert row["last_smtp_code"] is None
        assert row["last_error_class"] is None
        assert smtp_capture.delivery_count == 0

        # 执行体没有被这次挂起永久占住：下一轮（退避到点）照常把它发出去。
        smtp_capture.mode = "accept"
        recovered, final = await _attempt(
            session_factory, settings, message_id, now=row["next_attempt_at"]
        )
        assert recovered.sent == 1
        assert final["status"] == MESSAGE_STATE_SENT
        assert smtp_capture.delivery_count == 1
