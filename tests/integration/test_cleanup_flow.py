#!/usr/bin/env python3
"""流⑤ 保留期清理的集成用例（spec 0001 / `T-37` / `NFR-8` / `AC-22`）。

真源：`spec.md` 的 `AC-22` 原文（31 天前的邮件与其回复、31 天前的拒审行清理后查不到，29 天前的仍在；
**另有一条 31 天前但仍有入站待审行指向它的出站行，清理后必须仍在**）；`NFR-8`（保留 30 天）；
`design.md` §3.6（清理范围、`NOT EXISTS` 保护、回复行随父行级联、`pending_messages` 不参与清理）；
`plan.md` §5.5（分批口径：一批删满继续下一批，某批一条也删不掉时停止；被并发执行体锁走的行本轮跳过、
留给下一轮）与 §7.3 的 `AC-22` 一行（用例名与手法）；`AGENTS.md` §8.1（并发安全）与 §8.2（批次大小是配置项）。

**期望值不来自 `flows/cleanup.py` 与 `db/repositories/cleanup.py`**：保留期时长取自 `constants.RETENTION_DAYS`
（`NFR-8` 的产品语义，不是配置项）、批次大小由用例自己注入、取时点由注入的 `now` 固定
（`plan.md` §7.2：时间推进不靠 sleep）、行状态一律用裸 `SELECT` 读回。

**真实依赖**：PostgreSQL 是真的（T-33 夹具的运行级隔离库），清理流的会话工厂就是夹具的 `session_factory`
（生产口径：每批一个短会话、事务边界在流层）。本文件只在「另开一个会话持锁」的地方用第二个真实连接——
那是用例要考的并发面，不是替身。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vs_email_forwarding.config import Settings
from vs_email_forwarding.constants import (
    DEFAULT_BODY_FORMAT,
    DIRECTION_INBOUND,
    DIRECTION_OUTBOUND,
    REJECT_SOURCE_REVIEW_REJECTED,
    RETENTION_DAYS,
)
from vs_email_forwarding.db.models import Message, PendingMessage, RejectedMessage
from vs_email_forwarding.flows.cleanup import cleanup_tick

pytestmark = pytest.mark.integration

#: 本次清理的注入时刻（`plan.md` §7.2：时间推进不靠 sleep）。
_NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)

#: `NFR-8` 的保留期（产品语义常量，不是配置项）：`AC-22` 的 31 天 / 29 天都相对它取。
_RETENTION = timedelta(days=RETENTION_DAYS)

#: `AC-22` 的三条到期行：31 天前（保留期 30 天，因此已经到期）。
_EXPIRED = _NOW - timedelta(days=RETENTION_DAYS + 1)

#: `AC-22` 的对照行：29 天前，仍在保留期内。
_FRESH = _NOW - timedelta(days=RETENTION_DAYS - 1)

#: 非阻塞判据的墙钟上界：`SKIP LOCKED` 语义下被锁走的行本轮直接跳过；写成阻塞式
#: `FOR UPDATE` 时这里会一直等到锁释放（用例因此超时失败）。上界只判「有上界」，
#: 不判精确耗时（`docs/testing.md` R12）。
_CLAIM_WAIT_TIMEOUT_SECONDS = 2.0

_Factory = async_sessionmaker[AsyncSession]


async def _insert_message(
    factory: _Factory,
    *,
    created_at: datetime,
    direction: str = DIRECTION_OUTBOUND,
    parent_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """直接写一行 `messages`（`AC-22` 的造态手法：库中直接写入指定 `created_at` 的行）。

    出站行按表上的 CHECK 给全必填列；入站回复行带 `parent_id` 指回父行（`design.md` §2.1）。
    """
    row_id = uuid.uuid4()
    outbound = direction == DIRECTION_OUTBOUND
    async with factory() as session:
        await session.execute(
            sa.insert(Message).values(
                id=row_id,
                direction=direction,
                status="failed" if outbound else "received",
                message_id=f"<fixture-{row_id}@example.com>" if outbound else None,
                recipient="colleague@example.com",
                subject="夹具主题（无真实收件人）",
                body="夹具正文",
                parent_id=parent_id,
                mailbox=None if outbound else "reply@example.com",
                uidvalidity=None if outbound else 111,
                uid=None if outbound else 9000,
                created_at=created_at,
            )
        )
        await session.commit()
    return row_id


async def _insert_pending(factory: _Factory, *, parent_id: uuid.UUID) -> uuid.UUID:
    """写一行**入站**待审行指向给定出站行：`NOT EXISTS` 保护的构造前提（`design.md` §3.6）。"""
    row_id = uuid.uuid4()
    async with factory() as session:
        await session.execute(
            sa.insert(PendingMessage).values(
                id=row_id,
                direction=DIRECTION_INBOUND,
                message_id=None,
                recipient="replier@example.com",
                subject="待审回复夹具主题",
                body="待审回复夹具正文",
                body_format=DEFAULT_BODY_FORMAT,
                parent_id=parent_id,
                mailbox="reply@example.com",
                uidvalidity=111,
                uid=9100,
                attempt_count=0,
                next_attempt_at=_NOW,
                created_at=_EXPIRED,
            )
        )
        await session.commit()
    return row_id


async def _insert_rejected(factory: _Factory, *, created_at: datetime) -> uuid.UUID:
    """写一行拒审行（清理范围里的第二张表，`design.md` §3.6）。

    出站拒审行**不留内容**：表上的 `ck_rejected_messages_inbound_content` 要求出站行的
    subject / body 为 NULL（`design.md` §2.2），本用例要的是「这一行到期了吗」，内容列无关。
    """
    row_id = uuid.uuid4()
    async with factory() as session:
        await session.execute(
            sa.insert(RejectedMessage).values(
                id=row_id,
                direction=DIRECTION_OUTBOUND,
                source=REJECT_SOURCE_REVIEW_REJECTED,
                created_at=created_at,
                reviewed_at=created_at,
            )
        )
        await session.commit()
    return row_id


async def _row_ids(factory: _Factory, model: Any) -> set[uuid.UUID]:
    """把一张表的主键全读回来（裸 `SELECT`，不经被测代码）。"""
    async with factory() as session:
        return set((await session.scalars(sa.select(model.id))).all())


@asynccontextmanager
async def _counting_factory(factory: _Factory, rounds: list[int]) -> AsyncIterator[AsyncSession]:
    """包一层会话工厂：每开一个会话记一笔，用来数「本轮删了几批」（`plan.md` §5.5 的停止条件）。"""
    rounds.append(1)
    async with factory() as session:
        yield session


class TestCleanupScope:
    """`AC-22`：清理范围、保留期边界与 `NOT EXISTS` 保护（`plan.md` §7.3 的指定手法）。"""

    async def test_cleanup_scope_and_not_exists(
        self, session_factory: _Factory, integration_environment: Settings
    ) -> None:
        factory = session_factory
        # 31 天前的一对邮件（出站父行 + 它的入站回复行）。
        expired_parent = await _insert_message(factory, created_at=_EXPIRED)
        expired_reply = await _insert_message(
            factory, created_at=_EXPIRED, direction=DIRECTION_INBOUND, parent_id=expired_parent
        )
        # 31 天前的拒审行。
        expired_rejected = await _insert_rejected(factory, created_at=_EXPIRED)
        # 29 天前的对照行：仍在保留期内。
        fresh = await _insert_message(factory, created_at=_FRESH)
        # 31 天前、但仍有入站待审行指向它的出站行：必须留下，否则待审行会变成指向已删行的孤儿。
        protected = await _insert_message(factory, created_at=_EXPIRED)
        protected_pending = await _insert_pending(factory, parent_id=protected)

        result = await cleanup_tick(
            factory, integration_environment, retention=_RETENTION, now=_NOW
        )

        # 分表计数是 `CleanupResult` 的契约面：到期且无保护的 messages 行恰是那两条
        # （父行 + 回复行），拒审行恰是一条。
        assert (result.messages_deleted, result.rejected_deleted) == (2, 1)
        message_ids = await _row_ids(factory, Message)
        assert expired_parent not in message_ids
        assert expired_reply not in message_ids
        assert fresh in message_ids  # 29 天前仍在
        assert protected in message_ids  # NOT EXISTS 保护：仍有入站待审行指向它
        rejected_ids = await _row_ids(factory, RejectedMessage)
        assert expired_rejected not in rejected_ids
        # 待审行本身不参与清理（`pending_messages` 是纯队列），且没被留成孤儿。
        assert protected_pending in await _row_ids(factory, PendingMessage)

    async def test_pending_messages_are_never_deleted(
        self, session_factory: _Factory, integration_environment: Settings
    ) -> None:
        # 到期待审行（任何方向）都不在清理范围里：清理只碰 messages 与 rejected_messages。
        protected = await _insert_message(session_factory, created_at=_EXPIRED)
        pending_id = await _insert_pending(session_factory, parent_id=protected)

        result = await cleanup_tick(
            session_factory, integration_environment, retention=_RETENTION, now=_NOW
        )

        assert result.messages_deleted == 0
        assert pending_id in await _row_ids(session_factory, PendingMessage)


class TestCleanupBatching:
    """`plan.md` §5.5：一批删满就继续下一批，某批一条也删不掉时停止（批次大小取自配置）。"""

    async def test_batches_until_every_expired_row_is_gone(
        self, session_factory: _Factory, integration_environment: Settings
    ) -> None:
        expired = [await _insert_message(session_factory, created_at=_EXPIRED) for _ in range(5)]
        fresh = await _insert_message(session_factory, created_at=_FRESH)
        settings = integration_environment.model_copy(update={"batch_size": 2})
        rounds: list[int] = []

        @asynccontextmanager
        async def counting_factory() -> AsyncIterator[AsyncSession]:
            async with _counting_factory(session_factory, rounds) as session:
                yield session

        result = await cleanup_tick(counting_factory, settings, retention=_RETENTION, now=_NOW)

        assert result.messages_deleted == 5  # 单批只删 2 条，必须循环到删完
        message_ids = await _row_ids(session_factory, Message)
        assert all(row_id not in message_ids for row_id in expired)
        assert fresh in message_ids
        # 停止条件的推导（plan §5.5）：messages 侧 3 批（2 + 2 + 1）之后还要空跑一批确认「删 0 即停」，
        # rejected 侧本轮无到期行、空跑一批 —— 合计 5 个短会话、每个会话独立一个事务。
        assert len(rounds) == 5


class TestCleanupSkipLocked:
    """被并发执行体锁走的到期行：本轮跳过、不阻塞，留给下一轮（`design.md` §3.6 / `plan.md` §5.5）。"""

    async def test_locked_expired_row_is_left_for_the_next_round(
        self, session_factory: _Factory, integration_environment: Settings
    ) -> None:
        locked = await _insert_message(session_factory, created_at=_EXPIRED)
        other = await _insert_message(session_factory, created_at=_EXPIRED)
        # 另一个执行体：锁住一行、事务不结束（模拟「正在处理这一行」的并发执行体）。
        holder = session_factory()
        try:
            await holder.execute(
                sa.select(Message.id).where(Message.id == locked).with_for_update()
            )
            first = await asyncio.wait_for(
                cleanup_tick(
                    session_factory, integration_environment, retention=_RETENTION, now=_NOW
                ),
                timeout=_CLAIM_WAIT_TIMEOUT_SECONDS,
            )
            assert first.messages_deleted == 1  # 只删到没被锁的那一条
            message_ids = await _row_ids(session_factory, Message)
            assert locked in message_ids  # 被锁的行本轮不删
            assert other not in message_ids
        finally:
            await holder.rollback()
            await holder.close()
        # 下一轮（锁已释放）：那一行照常被删掉——它只是被跳过，不是被漏掉。
        second = await cleanup_tick(
            session_factory, integration_environment, retention=_RETENTION, now=_NOW
        )
        assert second.messages_deleted == 1
        assert locked not in await _row_ids(session_factory, Message)
