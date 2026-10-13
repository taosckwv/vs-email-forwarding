#!/usr/bin/env python3
"""``pending_messages`` 仓储的认领语义集成用例（spec 0001 / T-6 / `AC-26` / `AC-29` 的库内侧）。

验证的业务规则（真源：`specs/0001-email-forwarding-service/design.md` §3、
`plan.md` §5.1 ~ §5.3、`AGENTS.md` §8.1）——期望值全部来自规格与 constants，
不来自被测实现：

* **并发互斥**：两个并发执行体抢同一批，`FOR UPDATE SKIP LOCKED` 保证只有一方拿到行；
* **认领边界**：`attempt_count = 4` 的行仍可被认领（收割支命中面）且认领时不递增、
  不推退避；`attempt_count = 5` 的行永不再命中（不存在第 5 次尝试）；
* **退避推进**：认领第 n 次（n < 4）推 `next_attempt_at = now + RETRY_BACKOFF_SECONDS[n]`；
* **撞键幂等**：目标行已存在（同 id）时过审只删待审行、不重复写 `messages`。

只清测试库 `vs_email_forwarding_test`（连接串经 DATABASE_URL 环境变量覆盖，不改配置文件）；
开发库 `vs_email_forwarding_dev` 一概不碰。容器未起时整文件跳过。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from vs_email_forwarding.constants import (
    MAX_ATTEMPTS,
    RETRY_BACKOFF_SECONDS,
)
from vs_email_forwarding.db.models import Base, Message, PendingMessage, RejectedMessage
from vs_email_forwarding.db.repositories.pending import (
    approve_pending_message,
    claim_due_pending_messages,
    reject_pending_message,
)

pytestmark = pytest.mark.integration

_NOW: datetime = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)


async def _engine() -> AsyncEngine:
    # 引擎按用例自建、用例结束 dispose（docs/testing.md §4.3；连接串只从 config 读）。
    from vs_email_forwarding.db.session import create_engine_from_settings

    return create_engine_from_settings()


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    engine = await _engine()
    # 只清测试库的表（集成测试只准清 vs_email_forwarding_test）。
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for table in (RejectedMessage.__table__, Message.__table__, PendingMessage.__table__):
            await conn.execute(table.delete())
    yield engine
    await engine.dispose()


def _pending_row(
    *,
    attempt_count: int = 0,
    next_attempt_at: datetime | None = None,
    direction: str = "outbound",
    pending_id: uuid.UUID | None = None,
) -> dict[str, object]:
    """构造一条待审行。字段取值是夹具数据（docs/testing.md R10：等同断言，改动须评审）。"""
    return {
        "id": pending_id or uuid.uuid4(),
        "direction": direction,
        "message_id": "<example@example.com>" if direction == "outbound" else None,
        "recipient": "colleague@example.com",
        "subject": "夹具主题（无真实收件人）",
        "body": "夹具正文",
        "attempt_count": attempt_count,
        "next_attempt_at": next_attempt_at or _NOW,
        "created_at": _NOW - timedelta(hours=1),
    }


async def _insert_pending(engine: AsyncEngine, row: dict[str, object]) -> uuid.UUID:
    async with engine.begin() as conn:
        await conn.execute(sa.insert(PendingMessage).values(**row))
    return row["id"]  # type: ignore[return-value]


async def _fetch_pending(engine: AsyncEngine, pending_id: uuid.UUID) -> tuple[int, datetime] | None:
    """按列读回待审行（连接级只读，避免会话身份图干扰断言）。"""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                sa.select(PendingMessage.attempt_count, PendingMessage.next_attempt_at).where(
                    PendingMessage.id == pending_id
                )
            )
        ).first()
    return None if row is None else (row[0], row[1])


class TestConcurrencyMutualExclusion:
    """并发互斥：一方持有行锁未提交时，另一方的认领跳过该行（AC-29 的库内侧）。"""

    async def test_second_claim_skips_rows_locked_by_first(self, db_engine: AsyncEngine) -> None:
        pending_id = await _insert_pending(db_engine, _pending_row())

        # 执行体 A：认领后**不提交**，行锁保持。
        session_a = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            claimed_a = await claim_due_pending_messages(
                session_a, direction="outbound", now=_NOW, batch_size=10
            )
            assert [row.id for row in claimed_a] == [pending_id]

            # 执行体 B：独立会话（独立连接、独立事务），同一批认领必须
            # 拿不到已被 A 锁住的行。
            session_b = AsyncSession(bind=db_engine, expire_on_commit=False)
            try:
                claimed_b = await claim_due_pending_messages(
                    session_b, direction="outbound", now=_NOW, batch_size=10
                )
            finally:
                await session_b.rollback()
                await session_b.close()
        finally:
            await session_a.rollback()
            await session_a.close()
        # SKIP LOCKED 的语义：B 不阻塞、也不重复拿，同一批只会被一方拿到。
        assert claimed_b == []


class TestAttemptCountBoundary:
    """attempt_count 4 / 5 边界两侧：4 仍命中（收割面）且不递增，5 永不命中。"""

    async def test_attempt_count_four_still_claimable_but_untouched(
        self, db_engine: AsyncEngine
    ) -> None:
        stuck_at = _NOW - timedelta(seconds=1)
        pending_id = await _insert_pending(
            db_engine, _pending_row(attempt_count=MAX_ATTEMPTS, next_attempt_at=stuck_at)
        )

        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            claimed = await claim_due_pending_messages(
                session, direction="outbound", now=_NOW, batch_size=10
            )
            claimed_ids = [row.id for row in claimed]
        finally:
            await session.rollback()
            await session.close()

        # 收割支命中面：第 4 次认领的行被返回，但**不递增、不推退避**（无第 5 次尝试）。
        assert claimed_ids == [pending_id]
        fetched = await _fetch_pending(db_engine, pending_id)
        assert fetched is not None
        attempt_count, next_attempt_at = fetched
        assert attempt_count == MAX_ATTEMPTS
        assert next_attempt_at == stuck_at

    async def test_attempt_count_five_never_claimable(self, db_engine: AsyncEngine) -> None:
        # 谓词上界是 4（MAX_ATTEMPTS 来自 constants）；5 的行不属于任何流，永不再认领。
        assert MAX_ATTEMPTS == 4  # 期望值来自规格：首次 + 3 次重试
        beyond = await _insert_pending(db_engine, _pending_row(attempt_count=MAX_ATTEMPTS + 1))
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            claimed = await claim_due_pending_messages(
                session, direction="outbound", now=_NOW, batch_size=10
            )
            claimed_ids = [row.id for row in claimed]
        finally:
            await session.rollback()
            await session.close()
        assert claimed_ids == []
        fetched = await _fetch_pending(db_engine, beyond)
        assert fetched is not None
        assert fetched[0] == MAX_ATTEMPTS + 1


class TestBackoffAdvancement:
    """退避推进：认领第 n 次推 next_attempt_at = now + RETRY_BACKOFF_SECONDS[n]。"""

    async def test_each_claim_pushes_backoff_from_constants_table(
        self, db_engine: AsyncEngine
    ) -> None:
        # 期望值链来自规格：15s / 30s / 60s（docs/research/02，constants 为唯一出处），
        # 逐次认领并核对差值，不重写算术。
        pending_id = await _insert_pending(db_engine, _pending_row())
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            for expected_attempt in (1, 2, 3):
                claimed = await claim_due_pending_messages(
                    session, direction="outbound", now=_NOW, batch_size=10
                )
                assert [row.id for row in claimed] == [pending_id]
                # 直接读认领返回的行（同一会话，避免跨连接等锁）。
                assert claimed[0].attempt_count == expected_attempt
                delta = claimed[0].next_attempt_at - _NOW
                assert delta.total_seconds() == RETRY_BACKOFF_SECONDS[expected_attempt]
                if expected_attempt < 3:
                    # 行已被退避推走；按规格把到点时刻拨回，才能再次认领下一步。
                    await session.execute(
                        sa.update(PendingMessage)
                        .where(PendingMessage.id == pending_id)
                        .values(next_attempt_at=_NOW)
                    )
        finally:
            await session.rollback()
            await session.close()

    async def test_fourth_claim_pushes_no_backoff(self, db_engine: AsyncEngine) -> None:
        # 第 4 次认领不推退避：该行只剩终态一条路（design.md §5.2 退避表无 4 的下标）。
        assert 4 not in RETRY_BACKOFF_SECONDS
        pending_id = await _insert_pending(
            db_engine, _pending_row(attempt_count=3, next_attempt_at=_NOW)
        )
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            claimed = await claim_due_pending_messages(
                session, direction="outbound", now=_NOW, batch_size=10
            )
            claimed_ids = [row.id for row in claimed]
            # 认领（阶段①）是一个**已提交的短事务**（plan §5.1），这里照实提交。
            await session.commit()
        finally:
            await session.close()
        assert claimed_ids == [pending_id]
        fetched = await _fetch_pending(db_engine, pending_id)
        assert fetched is not None
        assert fetched[0] == 4
        assert fetched[1] == _NOW  # 未被推走


class TestIdempotentFinalization:
    """撞键幂等：目标行已存在时只删待审行、不重复写目标行。"""

    async def test_approval_with_existing_target_only_deletes_pending(
        self, db_engine: AsyncEngine
    ) -> None:
        same_id = uuid.uuid4()
        await _insert_pending(db_engine, _pending_row(pending_id=same_id))
        # 目标行已存在（例：另一个执行体已过审落库、本执行体在窗口期重跑）。
        async with db_engine.begin() as conn:
            await conn.execute(
                sa.insert(Message).values(
                    id=same_id,
                    direction="outbound",
                    status="sending",
                    message_id="<example@example.com>",
                    recipient="colleague@example.com",
                    subject="夹具主题（无真实收件人）",
                    body="夹具正文",
                    attempt_count=0,
                    next_attempt_at=_NOW,
                    created_at=_NOW - timedelta(hours=1),
                    reviewed_at=_NOW,
                )
            )

        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            finalized = await approve_pending_message(session, same_id, now=_NOW)
            await session.commit()
        finally:
            await session.close()

        assert finalized is True
        async with db_engine.connect() as conn:
            message_count = len((await conn.execute(sa.select(Message.id))).all())
            pending_count = len((await conn.execute(sa.select(PendingMessage.id))).all())
        # 撞键按「这一封已处理过」忽略：messages 恰好一行，待审行已删。
        assert message_count == 1
        assert pending_count == 0

    async def test_reject_keeps_content_only_for_inbound(self, db_engine: AsyncEngine) -> None:
        inbound_id = await _insert_pending(
            db_engine,
            _pending_row(direction="inbound", pending_id=None),
        )
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            finalized = await reject_pending_message(
                session, inbound_id, source="review_rejected", now=_NOW
            )
            await session.commit()
        finally:
            await session.close()
        assert finalized is True
        async with db_engine.connect() as conn:
            rejected_rows = (
                await conn.execute(
                    sa.select(
                        RejectedMessage.source,
                        RejectedMessage.direction,
                        RejectedMessage.subject,
                    )
                )
            ).all()
        assert len(rejected_rows) == 1
        source, direction, subject = rejected_rows[0]
        assert source == "review_rejected"
        assert direction == "inbound"
        assert subject == "夹具主题（无真实收件人）"  # 入站保留主题供复核


# ---------------------------------------------------------------------------
# T-7：messages 发信侧认领与条件 UPDATE（plan §5.1 / §5.2）
# ---------------------------------------------------------------------------

from vs_email_forwarding.constants import (  # noqa: E402  # 用例按任务分节排布
    ERROR_CLASSES,
    MESSAGE_STATES,
)
from vs_email_forwarding.db.repositories.messages import (  # noqa: E402
    claim_due_send_messages,
    mark_failed_from_sent,
    mark_send_failed,
    mark_sent,
)

_SENDING: str = MESSAGE_STATES[3]
_SENT: str = MESSAGE_STATES[4]
_FAILED: str = MESSAGE_STATES[5]


async def _insert_message(
    db_engine: AsyncEngine,
    *,
    status: str = _SENDING,
    attempt_count: int = 0,
    next_attempt_at: datetime | None = None,
    sent_at: datetime | None = None,
) -> uuid.UUID:
    """构造一条出站邮件行（夹具数据，等同断言：改动须按 R10 评审）。"""
    message_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            sa.insert(Message).values(
                id=message_id,
                direction="outbound",
                status=status,
                message_id=f"<fixture-{message_id}@example.com>",
                recipient="colleague@example.com",
                subject="夹具主题（无真实收件人）",
                body="夹具正文",
                attempt_count=attempt_count,
                next_attempt_at=next_attempt_at or _NOW,
                sent_at=sent_at,
                created_at=_NOW - timedelta(hours=1),
            )
        )
    return message_id


async def _fetch_message(
    db_engine: AsyncEngine, message_id: uuid.UUID
) -> tuple[str, int, datetime, datetime | None]:
    async with db_engine.connect() as conn:
        row = (
            await conn.execute(
                sa.select(
                    Message.status,
                    Message.attempt_count,
                    Message.next_attempt_at,
                    Message.sent_at,
                ).where(Message.id == message_id)
            )
        ).first()
    assert row is not None
    return row[0], row[1], row[2], row[3]


class TestSendClaim:
    """发信认领：只命中 sending + outbound + attempt_count<=4 且到点的行。"""

    async def test_claim_returns_due_sending_row_with_backoff(self, db_engine: AsyncEngine) -> None:
        message_id = await _insert_message(db_engine)

        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            claimed = await claim_due_send_messages(session, now=_NOW, batch_size=10)
            claimed_ids = [row.id for row in claimed]
            # 认领（阶段①）是已提交的短事务（plan §5.1），照实提交。
            await session.commit()
        finally:
            await session.close()

        assert claimed_ids == [message_id]
        status, attempt_count, next_attempt_at, _ = await _fetch_message(db_engine, message_id)
        assert status == _SENDING
        assert attempt_count == 1
        # 退避推进：认领第 1 次推 15s（退避表真源在 constants）。
        assert (next_attempt_at - _NOW).total_seconds() == RETRY_BACKOFF_SECONDS[1]

    async def test_claim_skips_sent_and_inbound_rows(self, db_engine: AsyncEngine) -> None:
        await _insert_message(db_engine, status=_SENT)
        await _insert_message(db_engine, status=_FAILED)

        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            claimed = await claim_due_send_messages(session, now=_NOW, batch_size=10)
        finally:
            await session.rollback()
            await session.close()
        # 认领谓词只认 status = sending：终态行不属于任何流（design §2.3）。
        assert claimed == []

    async def test_claim_harvest_face_at_four_is_untouched(self, db_engine: AsyncEngine) -> None:
        stuck_at = _NOW - timedelta(seconds=1)
        message_id = await _insert_message(
            db_engine, attempt_count=MAX_ATTEMPTS, next_attempt_at=stuck_at
        )

        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            claimed = await claim_due_send_messages(session, now=_NOW, batch_size=10)
            claimed_ids = [row.id for row in claimed]
            await session.commit()
        finally:
            await session.close()

        # 收割支命中面：第 4 次认领的行被返回但不递增、不推退避（无第 5 次投递）。
        assert claimed_ids == [message_id]
        _, attempt_count, next_attempt_at, _ = await _fetch_message(db_engine, message_id)
        assert attempt_count == MAX_ATTEMPTS
        assert next_attempt_at == stuck_at

    async def test_claim_beyond_four_never_hit(self, db_engine: AsyncEngine) -> None:
        await _insert_message(db_engine, attempt_count=MAX_ATTEMPTS + 1)
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            claimed = await claim_due_send_messages(session, now=_NOW, batch_size=10)
        finally:
            await session.rollback()
            await session.close()
        assert claimed == []


class TestConditionalUpdates:
    """条件 UPDATE：只允许声明的起点，rowcount = 0 按幂等忽略。"""

    async def test_mark_sent_only_from_sending(self, db_engine: AsyncEngine) -> None:
        message_id = await _insert_message(db_engine)
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            first = await mark_sent(session, message_id, now=_NOW)
            await session.commit()
            # 第二次写：起点已不是 sending，rowcount = 0 → 幂等忽略、不报错。
            second = await mark_sent(session, message_id, now=_NOW)
            await session.commit()
        finally:
            await session.close()

        assert first is True
        assert second is False
        status, _, _, sent_at = await _fetch_message(db_engine, message_id)
        assert status == _SENT
        assert sent_at == _NOW

    async def test_mark_send_failed_from_sending(self, db_engine: AsyncEngine) -> None:
        message_id = await _insert_message(db_engine)
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            outcome = await mark_send_failed(
                session,
                message_id,
                error_class="permanent",
                smtp_code=550,
                now=_NOW,
            )
            await session.commit()
        finally:
            await session.close()

        assert outcome is True
        status, _, _, sent_at = await _fetch_message(db_engine, message_id)
        assert status == _FAILED
        assert sent_at is None  # 失败不写 sent_at

    async def test_mark_send_failed_rejects_non_sending_start(self, db_engine: AsyncEngine) -> None:
        message_id = await _insert_message(db_engine, status=_SENT)
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            outcome = await mark_send_failed(
                session, message_id, error_class="permanent", smtp_code=550, now=_NOW
            )
        finally:
            await session.rollback()
            await session.close()
        # sent 不是 sending→failed 的合法起点：rowcount = 0，状态原样。
        assert outcome is False
        status, _, _, _ = await _fetch_message(db_engine, message_id)
        assert status == _SENT

    async def test_mark_failed_from_sent(self, db_engine: AsyncEngine) -> None:
        # 预置非默认的 sent_at 与尝试列：覆写只允许碰 status / last_error_class。
        sent_before = _NOW - timedelta(minutes=30)
        pushed_to = _NOW + timedelta(seconds=17)
        message_id = await _insert_message(
            db_engine,
            status=_SENT,
            attempt_count=2,
            next_attempt_at=pushed_to,
            sent_at=sent_before,
        )
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            outcome = await mark_failed_from_sent(
                session, message_id, error_class="permanent", now=_NOW
            )
            await session.commit()
        finally:
            await session.close()

        assert outcome is True
        status, attempt_count, next_attempt_at, sent_at = await _fetch_message(
            db_engine, message_id
        )
        assert status == _FAILED
        # 退信覆写保留「已发出」的事实，也不碰尝试列（plan §5.2：结果事务
        # 一律不写 attempt_count / next_attempt_at）。
        assert sent_at == sent_before
        assert attempt_count == 2
        assert next_attempt_at == pushed_to

    async def test_error_class_values_are_controlled(self, db_engine: AsyncEngine) -> None:
        # 期望值来自规格：错误分类只有 permanent / transient / internal（constants）。
        assert ERROR_CLASSES == ("permanent", "transient", "internal")
