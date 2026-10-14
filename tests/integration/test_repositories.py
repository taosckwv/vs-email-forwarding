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
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import TypedDict

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
        for model in (
            RejectedMessage,
            Message,
            PendingMessage,
            ImapCursor,
        ):
            await conn.execute(sa.delete(model))
    yield engine
    await engine.dispose()


class _PendingRow(TypedDict):
    """待审行夹具的字段契约：键名与待审行列一一对应，值是构造时的真实类型。"""

    id: uuid.UUID
    direction: str
    message_id: str | None
    recipient: str
    subject: str
    body: str
    attempt_count: int
    next_attempt_at: datetime
    created_at: datetime


def _pending_row(
    *,
    attempt_count: int = 0,
    next_attempt_at: datetime | None = None,
    direction: str = "outbound",
    pending_id: uuid.UUID | None = None,
) -> _PendingRow:
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


async def _insert_pending(engine: AsyncEngine, row: _PendingRow) -> uuid.UUID:
    async with engine.begin() as conn:
        await conn.execute(sa.insert(PendingMessage).values(**row))
    return row["id"]


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
            assert [row.id for row, _ in claimed_a] == [pending_id]

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
            claimed_rows = [(row.id, is_harvest) for row, is_harvest in claimed]
        finally:
            await session.rollback()
            await session.close()

        # 收割支命中面：第 4 次认领的行被返回、**不递增、不推退避**（无第 5 次尝试），
        # 并且带收割标志（调用方据此跳过外部调用，见 TestClaimHarvestFlag）。
        assert claimed_rows == [(pending_id, True)]
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
            claimed_ids = [row.id for row, _ in claimed]
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
                assert [(row.id, is_harvest) for row, is_harvest in claimed] == [
                    (pending_id, False)
                ]
                # 直接读认领返回的行（同一会话，避免跨连接等锁）。
                claimed_row, _ = claimed[0]
                assert claimed_row.attempt_count == expected_attempt
                delta = claimed_row.next_attempt_at - _NOW
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
            claimed_ids = [row.id for row, _ in claimed]
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

    async def test_reject_error_path_records_error_class(self, db_engine: AsyncEngine) -> None:
        # 审查出错用尽重试的落库口径（design.md §3.2 / §2.4）：source = review_error、
        # 分类 = internal。两者都是受控取值，真源是 constants；待审行上没有任何路径写
        # last_error_class，因此这一列必须由调用方从接缝显式给（对齐 messages.py 的
        # mark_send_failed(error_class=…)），否则 review_error 行该列恒空。
        assert "review_error" in REJECT_SOURCES
        assert "internal" in ERROR_CLASSES
        pending_id = await _insert_pending(db_engine, _pending_row())
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            finalized = await reject_pending_message(
                session,
                pending_id,
                source="review_error",
                error_class="internal",
                now=_NOW,
            )
            await session.commit()
        finally:
            await session.close()

        assert finalized is True
        async with db_engine.connect() as conn:
            row = (
                await conn.execute(
                    sa.select(RejectedMessage.source, RejectedMessage.last_error_class).where(
                        RejectedMessage.id == pending_id
                    )
                )
            ).one()
            pending_count = len((await conn.execute(sa.select(PendingMessage.id))).all())
        assert row[0] == "review_error"
        assert row[1] == "internal"
        assert pending_count == 0

    async def test_reject_without_error_class_leaves_it_null(self, db_engine: AsyncEngine) -> None:
        # 判定拒 / 收割路径没有「最后一次错误」：不给分类就不许编一个出来（design §2.4
        # 的 last_error_class 可为空）。
        pending_id = await _insert_pending(db_engine, _pending_row())
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            finalized = await reject_pending_message(
                session, pending_id, source="review_rejected", now=_NOW
            )
            await session.commit()
        finally:
            await session.close()

        assert finalized is True
        async with db_engine.connect() as conn:
            stored = (
                await conn.execute(
                    sa.select(RejectedMessage.last_error_class).where(
                        RejectedMessage.id == pending_id
                    )
                )
            ).one()
        assert stored[0] is None

    async def test_reject_error_class_must_be_controlled_value(
        self, db_engine: AsyncEngine
    ) -> None:
        # 越界分类在写入前即被拒（与本函数对 source 的校验同口径），不落任何行、不删待审行。
        pending_id = await _insert_pending(db_engine, _pending_row())
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            with pytest.raises(ValueError, match="error class"):
                await reject_pending_message(
                    session,
                    pending_id,
                    source="review_error",
                    error_class="not_a_class",
                    now=_NOW,
                )
        finally:
            await session.rollback()
            await session.close()

        async with db_engine.connect() as conn:
            rejected_count = len((await conn.execute(sa.select(RejectedMessage.id))).all())
            pending_count = len((await conn.execute(sa.select(PendingMessage.id))).all())
        assert rejected_count == 0
        assert pending_count == 1


class TestClaimHarvestFlag:
    """认领返回值必须逐行给出「本次是否按收割处理」的标志（`design.md` §3 阶段①）。

    认领之后两种行的列值**完全一样**（都是 `attempt_count == 4`、都不推退避），出库后
    再没有任何办法分辨；而流① / 流④ 对两者的处置恰恰相反：

    * 真·第 4 次尝试（认领前 3）必须**发起**审查调用——出错用尽重试才写
      `source = review_error`（`design.md` §3.2），不调用这一支就永远不可达；
    * 收割遗留行（认领前 4）**不得**发起调用，直接推进终态 `source = review_incomplete`
      （`design.md` §3 阶段①「不发起外部调用」、`docs/research/02`「不产生第 5 次调用」）。

    因此标志必须在认领这一层给出：认领方是唯一知道「刚才那一增是不是我加的」的地方。
    """

    async def test_fourth_attempt_and_harvest_leftover_are_told_apart(
        self, db_engine: AsyncEngine
    ) -> None:
        stuck_at = _NOW - timedelta(seconds=1)
        # 认领前 3：真·第 4 次尝试（本次认领递增到 4，随后必须发起调用）。
        fourth_id = await _insert_pending(
            db_engine, _pending_row(attempt_count=MAX_ATTEMPTS - 1, next_attempt_at=stuck_at)
        )
        # 认领前 4：第 4 次尝试已认领、结果事务未提交的崩溃遗留行（必须直接推进终态）。
        harvest_id = await _insert_pending(
            db_engine, _pending_row(attempt_count=MAX_ATTEMPTS, next_attempt_at=stuck_at)
        )

        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            claimed = await claim_due_pending_messages(
                session, direction="outbound", now=_NOW, batch_size=10
            )
            flags = {row.id: is_harvest for row, is_harvest in claimed}
            await session.commit()
        finally:
            await session.close()

        assert flags == {fourth_id: False, harvest_id: True}
        # SQL 语义不变：3 -> 4 递增、n=4 不推退避；收割行原样未动。
        assert await _fetch_pending(db_engine, fourth_id) == (MAX_ATTEMPTS, stuck_at)
        assert await _fetch_pending(db_engine, harvest_id) == (MAX_ATTEMPTS, stuck_at)
        # 两行的列值一模一样——这正是标志必须在仓储层给出的理由（本断言是接缝的存在证明）。
        assert await _fetch_pending(db_engine, fourth_id) == await _fetch_pending(
            db_engine, harvest_id
        )


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
            claimed_rows = [(row.id, is_harvest) for row, is_harvest in claimed]
            # 认领（阶段①）是已提交的短事务（plan §5.1），照实提交。
            await session.commit()
        finally:
            await session.close()

        assert claimed_rows == [(message_id, False)]
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
            claimed_rows = [(row.id, is_harvest) for row, is_harvest in claimed]
            await session.commit()
        finally:
            await session.close()

        # 收割支命中面：第 4 次认领的行被返回、不递增、不推退避（无第 5 次投递），
        # 并带收割标志（调用方据此跳过 SMTP，见 TestSendClaimHarvestFlag）。
        assert claimed_rows == [(message_id, True)]
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


class TestSendClaimHarvestFlag:
    """发信认领同口径：逐行给出收割标志（`AC-8` 与 `AC-37` 都依赖这一位）。

    `AC-8` 要求第 4 次尝试真的发出（失败后要有 `4xx` 返回码可记），`AC-37` 要求收割
    遗留行不产生第 5 次投递——认领之后两种行都是 `sending` + `attempt_count == 4`，
    标志是唯一判据。
    """

    async def test_fourth_attempt_and_harvest_leftover_are_told_apart(
        self, db_engine: AsyncEngine
    ) -> None:
        stuck_at = _NOW - timedelta(seconds=1)
        fourth_id = await _insert_message(
            db_engine, attempt_count=MAX_ATTEMPTS - 1, next_attempt_at=stuck_at
        )
        harvest_id = await _insert_message(
            db_engine, attempt_count=MAX_ATTEMPTS, next_attempt_at=stuck_at
        )

        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            claimed = await claim_due_send_messages(session, now=_NOW, batch_size=10)
            flags = {row.id: is_harvest for row, is_harvest in claimed}
            await session.commit()
        finally:
            await session.close()

        assert flags == {fourth_id: False, harvest_id: True}
        # SQL 语义不变：3 -> 4 递增、n=4 不推退避；收割行原样未动。
        assert await _fetch_message(db_engine, fourth_id) == (
            _SENDING,
            MAX_ATTEMPTS,
            stuck_at,
            None,
        )
        assert await _fetch_message(db_engine, harvest_id) == (
            _SENDING,
            MAX_ATTEMPTS,
            stuck_at,
            None,
        )
        # 两行的列值一模一样——标志是唯一判据（本断言是接缝的存在证明）。
        assert await _fetch_message(db_engine, fourth_id) == await _fetch_message(
            db_engine, harvest_id
        )


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


# ---------------------------------------------------------------------------
# T-8：rejected_messages 写入与存在性查询（design §2.1 / §3 流①④ 落库口径）
# ---------------------------------------------------------------------------

from vs_email_forwarding.constants import REJECT_SOURCES  # noqa: E402
from vs_email_forwarding.db.repositories.rejected import (  # noqa: E402
    insert_rejected_message,
    rejected_message_exists,
)


class TestRejectedInsertAndExists:
    """写入幂等 + 入站才保留主题与正文 + 按 ID 查存在性。"""

    async def test_outbound_reject_keeps_no_content(self, db_engine: AsyncEngine) -> None:
        rejected_id = uuid.uuid4()
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            inserted = await insert_rejected_message(
                session,
                rejected_id=rejected_id,
                direction="outbound",
                source="review_rejected",
                created_at=_NOW - timedelta(hours=1),
                reviewed_at=_NOW,
            )
            await session.commit()
        finally:
            await session.close()

        assert inserted is True
        async with db_engine.connect() as conn:
            row = (
                await conn.execute(
                    sa.select(
                        RejectedMessage.direction,
                        RejectedMessage.source,
                        RejectedMessage.subject,
                        RejectedMessage.body,
                        RejectedMessage.last_error_class,
                    ).where(RejectedMessage.id == rejected_id)
                )
            ).one()
        # 出站拒审不保留原文：主题与正文必须为 NULL（design §2.1）。
        assert row[0] == "outbound"
        assert row[1] == "review_rejected"
        assert row[2] is None
        assert row[3] is None
        assert row[4] is None

    async def test_inbound_reject_keeps_subject_and_body(self, db_engine: AsyncEngine) -> None:
        rejected_id = uuid.uuid4()
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            inserted = await insert_rejected_message(
                session,
                rejected_id=rejected_id,
                direction="inbound",
                source="review_error",
                subject="入站拒审主题",
                body="入站拒审正文（复核用）",
                last_error_class="internal",
                created_at=_NOW - timedelta(hours=1),
                reviewed_at=_NOW,
            )
            await session.commit()
        finally:
            await session.close()

        assert inserted is True
        async with db_engine.connect() as conn:
            row = (
                await conn.execute(
                    sa.select(
                        RejectedMessage.subject,
                        RejectedMessage.body,
                        RejectedMessage.last_error_class,
                    ).where(RejectedMessage.id == rejected_id)
                )
            ).one()
        # 入站拒审保留主题与正文供复核（design §2.1）。
        assert row[0] == "入站拒审主题"
        assert row[1] == "入站拒审正文（复核用）"
        assert row[2] == "internal"

    async def test_duplicate_insert_is_idempotent(self, db_engine: AsyncEngine) -> None:
        rejected_id = uuid.uuid4()
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            first = await insert_rejected_message(
                session,
                rejected_id=rejected_id,
                direction="outbound",
                source=REJECT_SOURCES[0],
                created_at=_NOW - timedelta(hours=1),
                reviewed_at=_NOW,
            )
            await session.commit()
            # 重跑同一条拒审：撞主键按幂等忽略，不报错、不产生第二行。
            second = await insert_rejected_message(
                session,
                rejected_id=rejected_id,
                direction="outbound",
                source=REJECT_SOURCES[0],
                created_at=_NOW - timedelta(hours=1),
                reviewed_at=_NOW,
            )
            await session.commit()
        finally:
            await session.close()

        assert first is True
        assert second is False
        async with db_engine.connect() as conn:
            count = len((await conn.execute(sa.select(RejectedMessage.id))).all())
        assert count == 1

    async def test_exists_queries_by_id(self, db_engine: AsyncEngine) -> None:
        rejected_id = uuid.uuid4()
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            assert await rejected_message_exists(session, rejected_id) is False
            await insert_rejected_message(
                session,
                rejected_id=rejected_id,
                direction="outbound",
                source=REJECT_SOURCES[0],
                created_at=_NOW - timedelta(hours=1),
                reviewed_at=_NOW,
            )
            await session.commit()
            assert await rejected_message_exists(session, rejected_id) is True
            assert await rejected_message_exists(session, uuid.uuid4()) is False
        finally:
            await session.close()

    async def test_source_must_be_controlled_value(self, db_engine: AsyncEngine) -> None:
        # source 只接受三个受控取值（constants 为唯一出处），越界在写入前被拒。
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            with pytest.raises(ValueError):
                await insert_rejected_message(
                    session,
                    rejected_id=uuid.uuid4(),
                    direction="outbound",
                    source="not_a_source",
                    created_at=_NOW - timedelta(hours=1),
                    reviewed_at=_NOW,
                )
        finally:
            await session.rollback()
            await session.close()


# ---------------------------------------------------------------------------
# T-9：imap_cursors 读取 / 初始化 / UIDVALIDITY 变化重置 / 同事务推进
# （design §2.2 游标表、plan §5.4 流③：撞唯一键不得把游标推进一起回滚）
# ---------------------------------------------------------------------------

from vs_email_forwarding.db.models import ImapCursor  # noqa: E402
from vs_email_forwarding.db.repositories.cursors import (  # noqa: E402
    advance_cursor,
    ensure_cursor,
    read_cursor,
)


class TestCursorEnsureAndRead:
    """初始化（UIDNEXT - 1）、重复 ensure 不重写、UIDVALIDITY 变化即重置。"""

    async def test_ensure_initializes_last_uid_to_uidnext_minus_one(
        self, db_engine: AsyncEngine
    ) -> None:
        # session 用 async with 托管，读回留在同一会话上下文内：close 之后再借连接
        # 不会归还池（docs/code-style.md §1 第 4 条）。
        async with AsyncSession(bind=db_engine, expire_on_commit=False) as session:
            state = await ensure_cursor(
                session, "reply@example.com", uidvalidity=111, uidnext=100, now=_NOW
            )
            await session.commit()

            # 初始 last_uid = UIDNEXT - 1：UID 是 1 起算的闭区间（design §2.2）。
            assert state == (111, 99)
            assert await read_cursor(session, "reply@example.com") == (111, 99)

    async def test_ensure_same_uidvalidity_keeps_last_uid(self, db_engine: AsyncEngine) -> None:
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            await ensure_cursor(
                session, "reply@example.com", uidvalidity=111, uidnext=100, now=_NOW
            )
            await session.execute(
                sa.update(ImapCursor)
                .where(ImapCursor.mailbox == "reply@example.com")
                .values(last_uid=150)
            )
            # 同一 UIDVALIDITY 再 ensure：这是「拉取前的例行走一次」，不得重置进度。
            state = await ensure_cursor(
                session, "reply@example.com", uidvalidity=111, uidnext=200, now=_NOW
            )
            await session.commit()
        finally:
            await session.close()
        assert state == (111, 150)

    async def test_ensure_with_new_uidvalidity_resets_last_uid(
        self, db_engine: AsyncEngine
    ) -> None:
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            await ensure_cursor(
                session, "reply@example.com", uidvalidity=111, uidnext=100, now=_NOW
            )
            await session.execute(
                sa.update(ImapCursor)
                .where(ImapCursor.mailbox == "reply@example.com")
                .values(last_uid=150)
            )
            # UIDVALIDITY 变化：信箱内容已被整体替换，进度作废、回到 UIDNEXT - 1。
            state = await ensure_cursor(
                session, "reply@example.com", uidvalidity=222, uidnext=50, now=_NOW
            )
            await session.commit()
        finally:
            await session.close()
        assert state == (222, 49)

    async def test_read_missing_cursor_returns_none(self, db_engine: AsyncEngine) -> None:
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            assert await read_cursor(session, "reply@example.com") is None
        finally:
            await session.close()


class TestCursorAdvance:
    """逐封同事务推进：只前进、不回退；撞唯一键不得把推进一起回滚（AC-11 / AC-39）。"""

    async def test_advance_is_monotonic(self, db_engine: AsyncEngine) -> None:
        # session 用 async with 托管，读回留在同一会话上下文内（docs/code-style.md §1 第 4 条）。
        async with AsyncSession(bind=db_engine, expire_on_commit=False) as session:
            await ensure_cursor(
                session, "reply@example.com", uidvalidity=111, uidnext=100, now=_NOW
            )
            first = await advance_cursor(
                session, "reply@example.com", uidvalidity=111, uid=105, now=_NOW
            )
            await session.commit()
            # 回退尝试：更小的 UID 不写行。
            backward = await advance_cursor(
                session, "reply@example.com", uidvalidity=111, uid=104, now=_NOW
            )
            # UIDVALIDITY 不匹配：不写行。
            foreign = await advance_cursor(
                session, "reply@example.com", uidvalidity=999, uid=200, now=_NOW
            )
            await session.commit()

            assert first is True
            assert backward is False
            assert foreign is False
            assert await read_cursor(session, "reply@example.com") == (111, 105)

    async def test_duplicate_insert_does_not_rollback_advance(self, db_engine: AsyncEngine) -> None:
        """同一事务内「撞唯一键的落库被幂等忽略 + 游标照常推进」一并提交（plan §5.4 流③）。"""
        # session 用 async with 托管，读回留在同一会话上下文内（docs/code-style.md §1 第 4 条）。
        async with AsyncSession(bind=db_engine, expire_on_commit=False) as session:
            await ensure_cursor(
                session, "reply@example.com", uidvalidity=111, uidnext=100, now=_NOW
            )
            await session.commit()

            # 预置一条已落待审表的入站行（占据唯一键 mailbox+uidvalidity+uid）；
            # 游标回退后流③ 会重取这封并再写一次——那正是撞唯一键的场景。
            existing_id = uuid.uuid4()
            async with db_engine.begin() as conn:
                await conn.execute(
                    sa.insert(PendingMessage).values(
                        id=existing_id,
                        direction="inbound",
                        recipient="reply@example.com",
                        subject="已有待审行",
                        body="已有正文",
                        mailbox="reply@example.com",
                        uidvalidity=111,
                        uid=101,
                        created_at=_NOW - timedelta(hours=1),
                    )
                )

            # 流③ 的同一事务：先写重复的待审行（被唯一键幂等挡住），再推进游标。
            duplicate_id = uuid.uuid4()
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            await session.execute(
                pg_insert(PendingMessage)
                .on_conflict_do_nothing(
                    index_elements=[
                        PendingMessage.mailbox,
                        PendingMessage.uidvalidity,
                        PendingMessage.uid,
                    ],
                    # 部分唯一索引只覆盖入站行（WHERE direction = 'inbound'）。
                    index_where=PendingMessage.direction == "inbound",
                )
                .values(
                    id=duplicate_id,
                    direction="inbound",
                    recipient="reply@example.com",
                    subject="重复回复",
                    body="重复正文",
                    mailbox="reply@example.com",
                    uidvalidity=111,
                    uid=101,
                    created_at=_NOW,
                )
            )
            advanced = await advance_cursor(
                session, "reply@example.com", uidvalidity=111, uid=101, now=_NOW
            )
            await session.commit()

            # 撞唯一键的插入被忽略（不抛出、不回滚），游标推进与提交一并生效。
            assert advanced is True
            assert await read_cursor(session, "reply@example.com") == (111, 101)
        async with db_engine.connect() as conn:
            pending_for_uid: Sequence[uuid.UUID] = (
                (
                    await conn.execute(
                        sa.select(PendingMessage.id).where(
                            PendingMessage.mailbox == "reply@example.com",
                            PendingMessage.uidvalidity == 111,
                            PendingMessage.uid == 101,
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert pending_for_uid == [existing_id]  # 重复行未写入，原行仍在


# ---------------------------------------------------------------------------
# T-10：保留期清理 —— 分批 + SKIP LOCKED + NOT EXISTS（plan §5.5 / NFR-8 / AC-22）
# ---------------------------------------------------------------------------

from vs_email_forwarding.db.repositories.cleanup import (  # noqa: E402
    cleanup_expired_messages,
    cleanup_expired_rejected,
)

_OLD: datetime = _NOW - timedelta(days=400)  # 早于任何保留期（夹具用，非保留期取值）
_NEW: datetime = _NOW + timedelta(hours=1)  # 晚于 cutoff（未到期）


async def _insert_message_full(
    db_engine: AsyncEngine,
    *,
    created_at: datetime,
    parent_id: uuid.UUID | None = None,
    direction: str = "outbound",
) -> uuid.UUID:
    message_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            sa.insert(Message).values(
                id=message_id,
                direction=direction,
                status="failed" if direction == "outbound" else "received",
                message_id=f"<fixture-{message_id}@example.com>"
                if direction == "outbound"
                else None,
                recipient="colleague@example.com",
                subject="夹具主题（无真实收件人）",
                body="夹具正文",
                parent_id=parent_id,
                mailbox="reply@example.com" if direction == "inbound" else None,
                uidvalidity=111 if direction == "inbound" else None,
                uid=9000 if direction == "inbound" else None,
                created_at=created_at,
            )
        )
    return message_id


async def _insert_pending_child(db_engine: AsyncEngine, parent_id: uuid.UUID) -> uuid.UUID:
    """构造一条仍指向出站父行的入站待审行（保护 NOT EXISTS 的考点）。"""
    pending_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            sa.insert(PendingMessage).values(
                id=pending_id,
                direction="inbound",
                parent_id=parent_id,
                recipient="reply@example.com",
                subject="待审回复",
                body="待审正文",
                mailbox="reply@example.com",
                uidvalidity=111,
                uid=8000,
                created_at=_NEW,
            )
        )
    return pending_id


async def _insert_rejected_row(db_engine: AsyncEngine, created_at: datetime) -> uuid.UUID:
    rejected_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            sa.insert(RejectedMessage).values(
                id=rejected_id,
                direction="outbound",
                source="review_rejected",
                created_at=created_at,
                reviewed_at=created_at,
            )
        )
    return rejected_id


async def _count(
    db_engine: AsyncEngine, model: type[Message] | type[PendingMessage] | type[RejectedMessage]
) -> int:
    async with db_engine.connect() as conn:
        return len((await conn.execute(sa.select(model.id))).all())


class TestCleanupBatching:
    """只删到期行；分批上限由入参控制；pending_messages 不参与清理。"""

    async def test_deletes_only_expired_and_respects_batch_size(
        self, db_engine: AsyncEngine
    ) -> None:
        for _ in range(3):
            await _insert_message_full(db_engine, created_at=_OLD)
        await _insert_message_full(db_engine, created_at=_NEW)  # 未到期，必须保留

        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            deleted = await cleanup_expired_messages(session, cutoff=_NOW, batch_size=2)
            await session.commit()
        finally:
            await session.close()

        # 批次上限 2 生效：一次 tick 只删 2 行（剩余留给下一轮）。
        assert deleted == 2
        remaining = await _count(db_engine, Message)
        assert remaining == 2  # 1 条未到期 + 1 条到期但未轮到

    async def test_nothing_expired_deletes_zero_rows(self, db_engine: AsyncEngine) -> None:
        await _insert_message_full(db_engine, created_at=_NEW)
        await _insert_rejected_row(db_engine, _NEW)

        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            deleted_messages = await cleanup_expired_messages(session, cutoff=_NOW, batch_size=10)
            deleted_rejected = await cleanup_expired_rejected(session, cutoff=_NOW, batch_size=10)
            await session.commit()
        finally:
            await session.close()

        # 写 0 行按幂等忽略、不报错（plan §5.4 流⑤）。
        assert deleted_messages == 0
        assert deleted_rejected == 0

    async def test_pending_messages_not_touched(self, db_engine: AsyncEngine) -> None:
        await _insert_pending_child(db_engine, parent_id=uuid.uuid4())
        before = await _count(db_engine, PendingMessage)

        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            await cleanup_expired_messages(session, cutoff=_NOW, batch_size=10)
            await cleanup_expired_rejected(session, cutoff=_NOW, batch_size=10)
            await session.commit()
        finally:
            await session.close()

        # pending_messages 是纯队列、不参与清理（plan §5.5）。
        assert await _count(db_engine, PendingMessage) == before


class TestCleanupProtection:
    """出站行仍有入站待审行指向时不删（NOT EXISTS）；拒审表独立清理。"""

    async def test_parent_with_pending_child_is_protected(self, db_engine: AsyncEngine) -> None:
        protected_parent = await _insert_message_full(db_engine, created_at=_OLD)
        unprotected_parent = await _insert_message_full(db_engine, created_at=_OLD)
        await _insert_pending_child(db_engine, protected_parent)

        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            deleted = await cleanup_expired_messages(session, cutoff=_NOW, batch_size=10)
            await session.commit()
        finally:
            await session.close()

        # 有入站待审行指向的出站行不删——否则流④ 写回复行会撞 parent_id 外键
        # 把正常回复误报成拒审（AC-22 后半段的考点）。
        assert deleted == 1
        async with db_engine.connect() as conn:
            still_there = (
                await conn.execute(sa.select(Message.id).where(Message.id == protected_parent))
            ).first()
            gone = (
                await conn.execute(sa.select(Message.id).where(Message.id == unprotected_parent))
            ).first()
        assert still_there is not None
        assert gone is None

    async def test_reply_rows_removed_with_parent_by_cascade(self, db_engine: AsyncEngine) -> None:
        parent = await _insert_message_full(db_engine, created_at=_OLD)
        # 回复行是入站行（direction 反向禁止：出站行不得有 parent_id）。
        await _insert_message_full(
            db_engine, created_at=_OLD, parent_id=parent, direction="inbound"
        )
        # 回复行不单独删：由 parent_id 外键级联清除（plan §5.5）。

        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            deleted = await cleanup_expired_messages(session, cutoff=_NOW, batch_size=10)
            await session.commit()
        finally:
            await session.close()

        # 父行与回复行都是 messages 行、都已到期，都会被选中；
        # 「回复不单独删」指的是不需要专门的回复清理语句（级联兜底）。
        assert deleted == 2
        assert await _count(db_engine, Message) == 0

    async def test_rejected_cleaned_independently(self, db_engine: AsyncEngine) -> None:
        old_rejected = await _insert_rejected_row(db_engine, _OLD)
        await _insert_rejected_row(db_engine, _NEW)

        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            deleted = await cleanup_expired_rejected(session, cutoff=_NOW, batch_size=10)
            await session.commit()
        finally:
            await session.close()

        assert deleted == 1
        async with db_engine.connect() as conn:
            remaining = (
                await conn.execute(
                    sa.select(RejectedMessage.id).where(RejectedMessage.id == old_rejected)
                )
            ).first()
        assert remaining is None


# ---------------------------------------------------------------------------
# T-11：messages 出站查询（六态判定输入）与回复 keyset 分页
# （design §4.1：排序固定 received_at ASC, id ASC；不用 limit/offset）
# ---------------------------------------------------------------------------

from vs_email_forwarding.db.repositories.messages import (  # noqa: E402
    message_status,
    pending_attempt_count,
    replies_page,
)


class TestMessageStateQueries:
    """六态判定的库内输入：messages.status、待审行 attempt_count。"""

    async def test_message_status_read(self, db_engine: AsyncEngine) -> None:
        message_id = await _insert_message_full(db_engine, created_at=_NEW)
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            status = await message_status(session, message_id)
            missing = await message_status(session, uuid.uuid4())
        finally:
            await session.close()
        assert status == "failed"
        assert missing is None

    async def test_pending_attempt_count_read(self, db_engine: AsyncEngine) -> None:
        # 已提交（attempt_count = 0）与审查中（attempt_count > 0）由待审行表达
        # （design §2.3），这里只提供库内输入，判定属 core（T-15）。
        row_zero = _pending_row(attempt_count=0)
        await _insert_pending(db_engine, row_zero)
        row_two = _pending_row(attempt_count=2)
        await _insert_pending(db_engine, row_two)

        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            zero = await pending_attempt_count(session, row_zero["id"])
            two = await pending_attempt_count(session, row_two["id"])
            missing = await pending_attempt_count(session, uuid.uuid4())
        finally:
            await session.close()

        assert zero == 0
        assert two == 2
        assert missing is None


class TestRepliesKeysetPagination:
    """回复 keyset 分页：received_at ASC, id ASC，翻页不重不漏（AC-15 / AC-16 / AC-41）。"""

    async def _seed_replies(
        self,
        db_engine: AsyncEngine,
        count: int,
        parent: uuid.UUID | None = None,
        *,
        uidvalidity: int = 777,
        uid_base: int = 7000,
        start_offset_min: int = 0,
    ) -> tuple[uuid.UUID, list[uuid.UUID]]:
        parent_id = parent or await _insert_message_full(db_engine, created_at=_NEW)
        ids: list[uuid.UUID] = []
        # 到达时间有并列（同一秒两封），排序必须落到 id 决胜。
        base = datetime(2025, 6, 1, 13, 0, 0, tzinfo=UTC) + timedelta(minutes=start_offset_min)
        for i in range(count):
            reply_id = uuid.uuid4()
            received = base + timedelta(minutes=i // 2)
            async with db_engine.begin() as conn:
                await conn.execute(
                    sa.insert(Message).values(
                        id=reply_id,
                        direction="inbound",
                        status="received",
                        parent_id=parent_id,
                        recipient="reply@example.com",
                        subject=f"回复 {i}",
                        body="回复正文",
                        mailbox="reply@example.com",
                        uidvalidity=uidvalidity,
                        uid=uid_base + i,
                        created_at=received,
                        received_at=received,
                    )
                )
            ids.append(reply_id)
        return parent_id, ids

    async def test_pages_cover_all_in_order_without_overlap(self, db_engine: AsyncEngine) -> None:
        parent_id, ids = await self._seed_replies(db_engine, 5)

        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            page1 = await replies_page(
                session,
                parent_id,
                after=(None, None),
                limit=2,
            )
            page2 = await replies_page(
                session, parent_id, after=(page1[-1].received_at, page1[-1].id), limit=2
            )
            page3 = await replies_page(
                session, parent_id, after=(page2[-1].received_at, page2[-1].id), limit=2
            )
        finally:
            await session.close()

        collected = [row.id for row in page1 + page2 + page3]
        # 期望值来自规格的排序键（received_at ASC, id ASC），按独立参考排序得出：
        # 并列 received_at 由 id 决胜，故不能假设插入顺序。
        assert set(collected) == set(ids)
        assert len(collected) == len(ids)
        received_order = [row.received_at for row in page1 + page2 + page3]
        assert received_order == sorted(received_order)
        # 同一 received_at 组内的 id 也必须升序（排序键的第二分量）。
        for i in range(1, len(page1 + page2 + page3)):
            prev = (page1 + page2 + page3)[i - 1]
            curr = (page1 + page2 + page3)[i]
            assert (prev.received_at, prev.id) < (curr.received_at, curr.id)
        # T-29 前置：页里交出的必须是**整行**，不是 (id, received_at) 两列投影——
        # `Reply` 契约要 subject/body/body_format（docs/api-contract.yaml）。
        assert all(isinstance(row, Message) for row in page1 + page2 + page3)
        assert sorted(row.subject for row in page1 + page2 + page3) == sorted(
            f"回复 {i}" for i in range(5)
        )
        assert {row.body for row in page1 + page2 + page3} == {"回复正文"}
        assert {row.body_format for row in page1 + page2 + page3} == {DEFAULT_BODY_FORMAT}
        assert {row.parent_id for row in page1 + page2 + page3} == {parent_id}

    async def test_unknown_parent_returns_empty_page(self, db_engine: AsyncEngine) -> None:
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            page = await replies_page(session, uuid.uuid4(), after=(None, None), limit=10)
        finally:
            await session.close()
        assert page == []

    async def test_new_reply_during_pagination_does_not_shift_window(
        self, db_engine: AsyncEngine
    ) -> None:
        parent_id, ids = await self._seed_replies(db_engine, 4)

        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            page1 = await replies_page(session, parent_id, after=(None, None), limit=2)
            # 翻页中途新回复到达（到达时刻晚于已取页；UIDVALIDITY 不同避免幂等键相撞）。
            new_ids = (
                await self._seed_replies(
                    db_engine,
                    1,
                    parent=parent_id,
                    uidvalidity=888,
                    uid_base=9000,
                    start_offset_min=10,
                )
            )[1]
            page2 = await replies_page(
                session, parent_id, after=(page1[-1].received_at, page1[-1].id), limit=10
            )
        finally:
            await session.close()

        # keyset 的偏移不漂移：已取过的页不重，新回复按到达时刻落在后续页。
        collected = {row.id for row in page1 + page2}
        assert collected == set(ids) | set(new_ids)
        assert new_ids[0] in {row.id for row in page2}
        assert len(page2) == 3  # 剩余 2 条旧回复 + 1 条新回复
        # T-29 前置：整行投影下内容列随行到手（旧回复「回复 2」「回复 3」+ 新回复「回复 0」），
        # 且父指向只落在本出站邮件上——两列投影时这两条即红。
        assert {row.body for row in page1 + page2} == {"回复正文"}
        assert {row.parent_id for row in page1 + page2} == {parent_id}
        assert sorted(row.subject for row in page2) == sorted(["回复 2", "回复 3", "回复 0"])


# ---------------------------------------------------------------------------
# T-12：入库幂等插入与两处入站唯一键
# （design §2.2「pending_messages 与 messages 各一份」、§3.4 流③ 落库、
# §3.5 流④ 过审落库；AC-11 的库内侧）
# ---------------------------------------------------------------------------

from vs_email_forwarding.constants import RECEIVED_STATE  # noqa: E402
from vs_email_forwarding.db.repositories.inbound import (  # noqa: E402
    ApproveInboundOutcome,
    approve_inbound_pending,
    insert_inbound_pending,
)

_UIDVALIDITY: int = 555  # 与其余节的夹具（111 / 777 / 888）错开，避免串节撞键


async def _insert_inbound_pending_row(
    db_engine: AsyncEngine,
    *,
    uid: int,
    uidvalidity: int = _UIDVALIDITY,
    mailbox: str = "reply@example.com",
    subject: str = "夹具回复主题",
    recipient: str = "reply@example.com",
    pending_id: uuid.UUID | None = None,
    parent_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """手工造一条入站待审行（夹具数据，docs/testing.md R10：等同断言，改动须评审）。

    ``parent_id`` 不传时取随机值：待审表无外键，仅过审搬运（messages 有 FK）时须传真实父行。
    """
    row_id = pending_id or uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            sa.insert(PendingMessage).values(
                id=row_id,
                direction="inbound",
                message_id=None,
                recipient=recipient,
                subject=subject,
                body="夹具回复正文",
                parent_id=parent_id or uuid.uuid4(),
                mailbox=mailbox,
                uidvalidity=uidvalidity,
                uid=uid,
                attempt_count=1,
                next_attempt_at=_NOW,
                created_at=_NOW - timedelta(hours=1),
            )
        )
    return row_id


class TestInboundPendingInsertIdempotency:
    """流③ 入库幂等（AC-11 库内侧）：同一 (mailbox, uidvalidity, uid) 只入库一次。"""

    async def test_second_fetch_of_same_uid_is_ignored(self, db_engine: AsyncEngine) -> None:
        # 游标回退后重拉同一封：三要素不变，第二次必须按幂等忽略（AC-11）。
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            first = await insert_inbound_pending(
                session,
                recipient="reply@example.com",
                subject="夹具回复主题",
                body="夹具回复正文",
                body_format="text",
                parent_id=uuid.uuid4(),
                mailbox="reply@example.com",
                uidvalidity=_UIDVALIDITY,
                uid=3001,
                now=_NOW,
            )
            # 重拉发生在之后：now 与内容都变了，也不得改写或追加。
            second = await insert_inbound_pending(
                session,
                recipient="other@example.com",
                subject="重拉后的主题",
                body="重拉后的正文",
                body_format="text",
                parent_id=uuid.uuid4(),
                mailbox="reply@example.com",
                uidvalidity=_UIDVALIDITY,
                uid=3001,
                now=_NOW + timedelta(hours=2),
            )
            await session.commit()
        finally:
            await session.close()

        assert first is True
        assert second is False
        async with db_engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.select(
                        PendingMessage.subject,
                        PendingMessage.recipient,
                        PendingMessage.created_at,
                        PendingMessage.next_attempt_at,
                    )
                )
            ).all()
        # 库中恰一行，且是第一次的数据：第二次不覆盖任何列（created_at 仍为入库时刻）。
        assert len(rows) == 1
        assert rows[0][0] == "夹具回复主题"
        assert rows[0][1] == "reply@example.com"
        assert rows[0][2] == _NOW
        assert rows[0][3] == _NOW

    async def test_different_uidvalidity_allows_new_row(self, db_engine: AsyncEngine) -> None:
        # 唯一键只管同一 UIDVALIDITY 内的重复（design §3.4「唯一键管同一 UIDVALIDITY 内
        # 的正确性」）：信箱轮换后同 UID 属于新邮件，必须可入库。
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            before = await insert_inbound_pending(
                session,
                recipient="reply@example.com",
                subject="轮换前",
                body="夹具回复正文",
                body_format="text",
                parent_id=uuid.uuid4(),
                mailbox="reply@example.com",
                uidvalidity=_UIDVALIDITY,
                uid=3002,
                now=_NOW,
            )
            after = await insert_inbound_pending(
                session,
                recipient="reply@example.com",
                subject="轮换后",
                body="夹具回复正文",
                body_format="text",
                parent_id=uuid.uuid4(),
                mailbox="reply@example.com",
                uidvalidity=_UIDVALIDITY + 1,
                uid=3002,
                now=_NOW,
            )
            await session.commit()
        finally:
            await session.close()

        assert before is True
        assert after is True
        async with db_engine.connect() as conn:
            count = len((await conn.execute(sa.select(PendingMessage.id))).all())
        assert count == 2


class TestInboundReceivedInsertIdempotency:
    """流④ 过审落库：messages 侧入站唯一键的终态兜底（design §2.2 说明第 3 条）。"""

    async def test_approve_writes_received_row_and_finalizes_pending(
        self, db_engine: AsyncEngine
    ) -> None:
        parent_id = await _insert_message_full(db_engine, created_at=_NOW - timedelta(hours=2))
        pending_id = await _insert_inbound_pending_row(db_engine, uid=3003, parent_id=parent_id)

        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            finalized = await approve_inbound_pending(session, pending_id, now=_NOW)
            await session.commit()
        finally:
            await session.close()

        assert finalized is ApproveInboundOutcome.APPROVED
        async with db_engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.select(
                        Message.status,
                        Message.created_at,
                        Message.received_at,
                        Message.reviewed_at,
                        Message.attempt_count,
                        Message.next_attempt_at,
                        Message.parent_id,
                        Message.uidvalidity,
                        Message.uid,
                    ).where(Message.id == pending_id)
                )
            ).all()
            pending_count = len((await conn.execute(sa.select(PendingMessage.id))).all())
        # 跨表 ID 同值一行 received；created_at 原样搬运（§2.5：不得重新取 now 覆盖），
        # received_at / reviewed_at / next_attempt_at 取绑定的 now，attempt_count 重置 0。
        assert len(rows) == 1
        (
            status,
            created_at,
            received_at,
            reviewed_at,
            attempt_count,
            next_attempt_at,
            parent,
            validity,
            uid,
        ) = rows[0]
        assert status == RECEIVED_STATE
        assert created_at == _NOW - timedelta(hours=1)
        assert received_at == _NOW
        assert reviewed_at == _NOW
        assert attempt_count == 0
        assert next_attempt_at == _NOW
        assert parent == parent_id
        assert validity == _UIDVALIDITY
        assert uid == 3003
        # 待审行同事务删除：库中不再有待审行。
        assert pending_count == 0

    async def test_approve_with_existing_target_row_ignores_duplicate(
        self, db_engine: AsyncEngine
    ) -> None:
        # messages 侧唯一键的兜底场景：同一封回复的 messages 行已存在（例：上次过审
        # 落库后游标回退又重拉出一行待审），过审不得写出第二条 received 行。
        parent_id = await _insert_message_full(db_engine, created_at=_NOW - timedelta(hours=2))
        pending_id = await _insert_inbound_pending_row(db_engine, uid=3004, parent_id=parent_id)
        earlier = _NOW - timedelta(minutes=30)
        async with db_engine.begin() as conn:
            await conn.execute(
                sa.insert(Message).values(
                    id=uuid.uuid4(),
                    direction="inbound",
                    status="received",
                    parent_id=parent_id,
                    recipient="reply@example.com",
                    subject="先落库的回复",
                    body="夹具回复正文",
                    mailbox="reply@example.com",
                    uidvalidity=_UIDVALIDITY,
                    uid=3004,
                    created_at=earlier,
                    received_at=earlier,
                )
            )

        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            finalized = await approve_inbound_pending(session, pending_id, now=_NOW)
            await session.commit()
        finally:
            await session.close()

        # 待审行已收尾，但 messages 恰一行、且是先落库那条（received_at 未被覆盖）。
        assert finalized is ApproveInboundOutcome.APPROVED
        async with db_engine.connect() as conn:
            received_rows = (
                await conn.execute(
                    sa.select(Message.received_at, Message.subject).where(
                        Message.direction == "inbound"
                    )
                )
            ).all()
            pending_count = len((await conn.execute(sa.select(PendingMessage.id))).all())
        assert len(received_rows) == 1
        assert received_rows[0][0] == earlier
        assert received_rows[0][1] == "先落库的回复"
        assert pending_count == 0

    async def test_approve_missing_pending_returns_false(self, db_engine: AsyncEngine) -> None:
        # 待审行不存在（已被其他执行体收尾）时按幂等忽略，不写任何行。
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            finalized = await approve_inbound_pending(session, uuid.uuid4(), now=_NOW)
            await session.commit()
        finally:
            await session.close()

        assert finalized is ApproveInboundOutcome.PENDING_GONE
        async with db_engine.connect() as conn:
            message_count = len((await conn.execute(sa.select(Message.id))).all())
            pending_count = len((await conn.execute(sa.select(PendingMessage.id))).all())
        assert message_count == 0
        assert pending_count == 0


class TestInboundReplyParentGoneDiscard:
    """T-26 前置：父行已被保留期清理时的「丢弃」接缝（design §3.5 ``reply_target_gone`` + §6）。"""

    async def test_approve_after_parent_purged_discards_pending_row(
        self, db_engine: AsyncEngine
    ) -> None:
        # 场景：回复已落待审行，父行随后被保留期清理（清理流的 NOT EXISTS 只在不撞并发
        # 窗口时挡住这一情形）。此时过审必须按 reply_target_gone 丢弃：删待审行、零目标行。
        parent_id = await _insert_message_full(db_engine, created_at=_NOW - timedelta(hours=2))
        pending_id = await _insert_inbound_pending_row(db_engine, uid=3005, parent_id=parent_id)
        async with db_engine.begin() as conn:
            await conn.execute(sa.delete(Message).where(Message.id == parent_id))

        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            outcome = await approve_inbound_pending(session, pending_id, now=_NOW)
            await session.commit()
        finally:
            await session.close()

        # 三态可区分：父行已清理给 PARENT_GONE，既不是 APPROVED 也不是 PENDING_GONE。
        assert outcome is ApproveInboundOutcome.PARENT_GONE
        async with db_engine.connect() as conn:
            message_count = len((await conn.execute(sa.select(Message.id))).all())
            rejected_count = len((await conn.execute(sa.select(RejectedMessage.id))).all())
            pending_count = len((await conn.execute(sa.select(PendingMessage.id))).all())
        # 零目标行：不写 received（父行已不在，写不出来）、也不写 rejected
        # （§6 明令否掉「把用户的正常回复误报成拒审」）。
        assert message_count == 0
        assert rejected_count == 0
        # 待审行被丢弃：留下它会被反复认领，第 4 次由收割支写进 rejected_messages。
        assert pending_count == 0

    async def test_missing_pending_with_purged_parent_reports_pending_gone(
        self, db_engine: AsyncEngine
    ) -> None:
        # 分支顺序：待审行不在就先按幂等忽略，不得因为「库是空的、父行查不到」而报丢弃。
        missing_parent = uuid.uuid4()
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            outcome = await approve_inbound_pending(session, uuid.uuid4(), now=_NOW)
            await session.commit()
        finally:
            await session.close()

        assert outcome is ApproveInboundOutcome.PENDING_GONE
        assert outcome is not ApproveInboundOutcome.PARENT_GONE
        async with db_engine.connect() as conn:
            message_count = len((await conn.execute(sa.select(Message.id))).all())
            pending_count = len((await conn.execute(sa.select(PendingMessage.id))).all())
            rejected_count = len((await conn.execute(sa.select(RejectedMessage.id))).all())
            still_there = (
                await conn.execute(sa.select(Message.id).where(Message.id == missing_parent))
            ).all()
        assert (message_count, pending_count, rejected_count) == (0, 0, 0)
        assert still_there == []


# ---------------------------------------------------------------------------
# T-25 前置：按 Message-ID 读出站行（流③ 回复支与 DSN 退信支共用的只读口）
# 真源：design §2.2 的 message_id 列口径（仅出站行、全局唯一）与
# §3.3「退信到达」一行（靠 sent_at 是否为空分 dsn_target_not_sent / 置 failed）
# ---------------------------------------------------------------------------

from vs_email_forwarding.db.repositories.messages import (  # noqa: E402
    outbound_targets_by_message_id,
)


async def _insert_outbound_row(
    db_engine: AsyncEngine,
    *,
    message_id: str,
    sent_at: datetime | None,
) -> uuid.UUID:
    """造一条带指定 Message-ID 的出站行（夹具数据，docs/testing.md R10：改动须评审）。

    ``sent_at`` 是必填夹具参数：它区分「曾被 MTA 接受」与「尚未发出」，正是本读口
    返回值里第二位分量的考点，不允许由被造对象默认（出站行没有 sent_at 默认值）。
    """
    row_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            sa.insert(Message).values(
                id=row_id,
                direction="outbound",
                status=_SENT if sent_at is not None else _SENDING,
                message_id=message_id,
                recipient="colleague@example.com",
                subject="夹具主题（无真实收件人）",
                body="夹具正文",
                attempt_count=0,
                next_attempt_at=_NOW,
                sent_at=sent_at,
                created_at=_NOW - timedelta(hours=1),
            )
        )
    return row_id


class TestOutboundTargetsByMessageId:
    """按 Message-ID 读出站行：命中映射、未命中空字典、只认出站行。"""

    async def test_hits_map_message_id_to_row_id_and_sent_at(self, db_engine: AsyncEngine) -> None:
        accepted_at = _NOW - timedelta(minutes=5)
        sent_row = await _insert_outbound_row(
            db_engine, message_id="<t25-hit-sent@example.com>", sent_at=accepted_at
        )
        # 尚未发出的行：sent_at 为空——退信支据此走 dsn_target_not_sent（§3.3）。
        unsent_row = await _insert_outbound_row(
            db_engine, message_id="<t25-hit-sending@example.com>", sent_at=None
        )
        # 未被点名的出站行不得进入结果：字典键恰好是入参里能命中的那些。
        await _insert_outbound_row(
            db_engine, message_id="<t25-not-queried@example.com>", sent_at=_NOW
        )

        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            targets = await outbound_targets_by_message_id(
                session,
                ["<t25-hit-sending@example.com>", "<t25-hit-sent@example.com>"],
            )
        finally:
            await session.close()

        # 期望值就是夹具里写死的 (行 UUID, sent_at) 对——不从被测实现取任何值。
        assert targets == {
            "<t25-hit-sent@example.com>": (sent_row, accepted_at),
            "<t25-hit-sending@example.com>": (unsent_row, None),
        }

    async def test_unknown_message_id_returns_empty_mapping(self, db_engine: AsyncEngine) -> None:
        await _insert_outbound_row(db_engine, message_id="<t25-present@example.com>", sent_at=_NOW)

        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            # 库里有出站行、但没有任何一行用这个 Message-ID（例：DSN 指向的信已被清理，
            # §3.3 的 dsn_target_gone 支）。
            absent = await outbound_targets_by_message_id(session, ["<t25-absent@example.com>"])
            # 候选集合为空（流③ 一个标识都没抽出来）：仍是空结果，不得退化成「查全部」。
            nothing_asked = await outbound_targets_by_message_id(session, [])
        finally:
            await session.close()

        assert absent == {}
        assert nothing_asked == {}

    async def test_inbound_rows_are_never_returned(self, db_engine: AsyncEngine) -> None:
        """只认出站行：入站行即使存在也不会成为关联目标。

        ``messages`` 的 CHECK（`ck_messages_direction_columns`）强制入站行
        ``message_id`` 为 NULL，因此本读口的 direction 谓词是纵深防御；本用例钉住的
        可观察行为是「入站行永不进入结果」（design §2.2：`message_id` 只装出站行的
        对外标识，入站回复靠 `parent_id` 外键关联）。
        """
        outbound_row = await _insert_outbound_row(
            db_engine, message_id="<t25-only-outbound@example.com>", sent_at=_NOW
        )
        inbound_row = await _insert_message_full(db_engine, created_at=_NOW, direction="inbound")

        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            targets = await outbound_targets_by_message_id(
                session,
                ["<t25-only-outbound@example.com>", f"<t25-{inbound_row}@example.com>"],
            )
        finally:
            await session.close()

        assert targets == {"<t25-only-outbound@example.com>": (outbound_row, _NOW)}
        # 夹具前提：入站行确实在库里（不是「没造出来」才没进结果）。
        async with db_engine.connect() as conn:
            stored_inbound = (
                await conn.execute(sa.select(Message.id).where(Message.id == inbound_row))
            ).first()
        assert stored_inbound is not None


# ---------------------------------------------------------------------------
# T-28 前置：出站待审行的建行接缝
# ---------------------------------------------------------------------------

from vs_email_forwarding.db.repositories.pending import (  # noqa: E402  # 用例按任务分节排布
    insert_outbound_pending,
)


class TestOutboundPendingInsert:
    """提交建行（`design.md` §3.1 / §2.4 / §2.5）：一行可被流① 认领的出站待审行。

    期望值全部来自规格字面量，不来自被测实现：``id`` 即主键、``message_id`` 由提交方
    按该 ``id`` 与发件域生成（§3.1）；``attempt_count`` 建行即 0、``next_attempt_at``
    建行即当前时刻（§2.4）；``created_at`` 取提交时刻，`NFR-8` 的 30 天保留期自它起算
    （§2.5）；入站三要素与 ``parent_id`` 全空——出站行没有 IMAP 来源（§2.1 / §2.2）。
    """

    #: 提交方新生成的对外 ID（也是主键）与它对应的 Message-ID（夹具数据，R10）。
    _PENDING_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
    _MESSAGE_ID = "<33333333-3333-4333-8333-333333333333@example.com>"

    async def _build_row(self, db_engine: AsyncEngine, *, body_format: str = "text") -> None:
        """走被测接缝建一行出站待审行并提交（受控的提交侧调用形状）。"""
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            await insert_outbound_pending(
                session,
                pending_id=self._PENDING_ID,
                message_id=self._MESSAGE_ID,
                recipient="colleague@example.com",
                subject="夹具主题（无真实收件人）",
                body="夹具正文",
                body_format=body_format,
                now=_NOW,
            )
            await session.commit()
        finally:
            await session.close()

    async def test_writes_row_with_expected_columns(self, db_engine: AsyncEngine) -> None:
        await self._build_row(db_engine)

        async with db_engine.connect() as conn:
            row = (
                await conn.execute(
                    sa.select(
                        PendingMessage.direction,
                        PendingMessage.message_id,
                        PendingMessage.recipient,
                        PendingMessage.subject,
                        PendingMessage.body,
                        PendingMessage.body_format,
                        PendingMessage.parent_id,
                        PendingMessage.mailbox,
                        PendingMessage.uidvalidity,
                        PendingMessage.uid,
                        PendingMessage.attempt_count,
                        PendingMessage.next_attempt_at,
                        PendingMessage.created_at,
                        PendingMessage.last_error_class,
                    ).where(PendingMessage.id == self._PENDING_ID)
                )
            ).one()

        assert row.direction == "outbound"
        assert row.message_id == self._MESSAGE_ID
        assert row.recipient == "colleague@example.com"
        assert row.subject == "夹具主题（无真实收件人）"
        assert row.body == "夹具正文"
        assert row.body_format == "text"
        # 入站三要素与父行必须留空（出站行没有 IMAP 来源，表上的行模型 CHECK 兜底）。
        assert row.parent_id is None
        assert row.mailbox is None
        assert row.uidvalidity is None
        assert row.uid is None
        # 建行初值：尚未认领（0 次尝试），两个时间列都取入参 now——若实现改用 func.now()，
        # 拿到的是真实当前时刻（远晚于夹具的 2025-06-01），这两条即红。
        assert row.attempt_count == 0
        assert row.next_attempt_at == _NOW
        assert row.created_at == _NOW
        assert row.last_error_class is None

    async def test_new_row_is_claimable_as_outbound(self, db_engine: AsyncEngine) -> None:
        await self._build_row(db_engine)

        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            claimed = await claim_due_pending_messages(
                session, direction="outbound", now=_NOW, batch_size=10
            )
        finally:
            await session.close()

        # 建行即到点（next_attempt_at = now）且 attempt_count = 0 < 4：流① 当轮即可认领，
        # 且不是收割支（design §3 阶段①）。
        assert [(row.id, is_harvest) for row, is_harvest in claimed] == [(self._PENDING_ID, False)]

    async def test_new_row_is_invisible_to_inbound_claim(self, db_engine: AsyncEngine) -> None:
        await self._build_row(db_engine)

        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            claimed_inbound = await claim_due_pending_messages(
                session, direction="inbound", now=_NOW, batch_size=10
            )
        finally:
            await session.close()

        # 方向隔离：出站建行不得混进流④ 的入站队列。
        assert claimed_inbound == []
        # 夹具前提：那一行确实在库里（不是「没建出来」才认领不到）。
        async with db_engine.connect() as conn:
            stored = (
                await conn.execute(
                    sa.select(PendingMessage.direction).where(PendingMessage.id == self._PENDING_ID)
                )
            ).first()
        assert stored is not None
        assert stored[0] == "outbound"

    async def test_body_format_is_written_from_caller(self, db_engine: AsyncEngine) -> None:
        await self._build_row(db_engine, body_format="html")

        async with db_engine.connect() as conn:
            stored = (
                await conn.execute(
                    sa.select(PendingMessage.body_format).where(
                        PendingMessage.id == self._PENDING_ID
                    )
                )
            ).scalar_one()

        # html 必须原样入库：写死 text、或漏写该列而落到 server_default 'text'，都会被这条钉住
        # （取值合法性由 schema 层保证，仓储只做搬运）。
        assert stored == "html"


# ---------------------------------------------------------------------------
# T-29 前置：只读整行接缝与可配置隔离级别
# （design §8 仓储把 ORM 行交给 service；§4.1「三次查询同一快照」靠按次覆盖
#  REPEATABLE READ；plan.md §2.9。本节的四个读入口与 session_factory 参数
#  专供 T-29 的查询用例，不经由写路径使用）
# ---------------------------------------------------------------------------

from vs_email_forwarding.constants import DEFAULT_BODY_FORMAT  # noqa: E402
from vs_email_forwarding.db.repositories.messages import message_row  # noqa: E402
from vs_email_forwarding.db.repositories.pending import pending_row  # noqa: E402
from vs_email_forwarding.db.repositories.rejected import rejected_row  # noqa: E402
from vs_email_forwarding.db.session import dispose_engine, session_factory  # noqa: E402


class TestMessageRowRead:
    """`message_row`：按 ID 交出整行（判定输入 + 响应字段都够用，design §4.1 / §8）。"""

    async def test_returns_whole_outbound_row(self, db_engine: AsyncEngine) -> None:
        message_id = await _insert_message_full(db_engine, created_at=_NEW)

        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            row = await message_row(session, message_id)
            # 只读：读完会话里没有被标记为脏的对象（不写库、不加锁）。
            nothing_dirty = not session.dirty
        finally:
            await session.close()

        assert row is not None
        assert row.id == message_id
        # 判定输入：direction 是 RowFacts 的必填字段（core/states.py:66）。
        assert row.direction == "outbound"
        assert row.status == "failed"
        # 响应字段：MessageStatusResponse.required 里的三个时间列（docs/api-contract.yaml）。
        assert row.created_at == _NEW
        assert row.sent_at is None  # 夹具未设 sent_at
        assert row.reviewed_at is None
        # 内容列随整行一起到手——只返回 (id, status) 形态时这三条即红。
        assert row.subject == "夹具主题（无真实收件人）"
        assert row.body == "夹具正文"
        assert row.body_format == DEFAULT_BODY_FORMAT
        assert nothing_dirty

    async def test_returns_inbound_row_with_direction_visible(self, db_engine: AsyncEngine) -> None:
        # 入站回复行也按整行交出：六态判定靠 direction 分流到 received（design §2.3）。
        message_id = await _insert_message_full(db_engine, created_at=_NEW, direction="inbound")

        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            row = await message_row(session, message_id)
        finally:
            await session.close()

        assert row is not None
        assert row.direction == "inbound"
        assert row.status == RECEIVED_STATE

    async def test_missing_id_returns_none(self, db_engine: AsyncEngine) -> None:
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            row = await message_row(session, uuid.uuid4())
        finally:
            await session.close()
        # 「该 ID 不存在」是正常结果，不是错误（design §4.1、AC-35）。
        assert row is None


class TestPendingRowRead:
    """`pending_row`：按 ID 交出整行待审行（attempt_count + created_at 都够用）。"""

    async def test_returns_whole_pending_row(self, db_engine: AsyncEngine) -> None:
        fixture = _pending_row(attempt_count=2)
        await _insert_pending(db_engine, fixture)

        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            row = await pending_row(session, fixture["id"])
            nothing_dirty = not session.dirty
        finally:
            await session.close()

        assert row is not None
        assert row.id == fixture["id"]
        # 判定输入：attempt_count 是「已提交 / 审查中」的唯一库内判据（design §2.3）。
        assert row.attempt_count == 2
        assert row.direction == "outbound"
        # 响应字段与内容列（created_at 是 NFR-8 保留期起算点，design §2.5）。
        assert row.created_at == fixture["created_at"]
        assert row.next_attempt_at == fixture["next_attempt_at"]
        assert row.subject == fixture["subject"]
        assert row.body == fixture["body"]
        assert row.message_id == fixture["message_id"]
        assert nothing_dirty

    async def test_missing_id_returns_none(self, db_engine: AsyncEngine) -> None:
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            row = await pending_row(session, uuid.uuid4())
        finally:
            await session.close()
        assert row is None


class TestRejectedRowRead:
    """`rejected_row`：按 ID 交出整行拒审行（created_at + reviewed_at 都够用）。"""

    async def test_returns_whole_rejected_row(self, db_engine: AsyncEngine) -> None:
        rejected_id = await _insert_rejected_row(db_engine, _NEW)

        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            row = await rejected_row(session, rejected_id)
            nothing_dirty = not session.dirty
        finally:
            await session.close()

        assert row is not None
        assert row.id == rejected_id
        assert row.direction == "outbound"
        assert row.source == "review_rejected"
        # 响应字段：created_at（提交时刻）与 reviewed_at（审查结束时刻）。
        assert row.created_at == _NEW
        assert row.reviewed_at == _NEW
        assert nothing_dirty

    async def test_missing_id_returns_none(self, db_engine: AsyncEngine) -> None:
        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            row = await rejected_row(session, uuid.uuid4())
        finally:
            await session.close()
        assert row is None


class TestQuerySnapshotSession:
    """design §4.1「三次查询同一快照」的落地机制：只读会话按次覆盖为 REPEATABLE READ。"""

    @pytest.fixture(autouse=True)
    async def _per_test_singleton(self) -> AsyncIterator[None]:
        # `session_factory` 绑的是进程级单例引擎，而 pytest-asyncio 每个用例一个新事件循环：
        # 上一个用例留下的池化连接会挂到已关闭的 loop（Task attached to a different loop）。
        # 用前先释放，让工厂在本用例的 loop 里重建；用例结束再释放（docs/testing.md §4.3）。
        await dispose_engine()
        yield
        await dispose_engine()

    async def test_isolation_override_reaches_the_server(self, db_engine: AsyncEngine) -> None:
        override = session_factory(isolation_level="REPEATABLE READ")()
        default = session_factory()()
        try:
            override_level = (
                await override.execute(sa.text("SHOW transaction_isolation"))
            ).scalar_one()
            default_level = (
                await default.execute(sa.text("SHOW transaction_isolation"))
            ).scalar_one()
        finally:
            await override.close()
            await default.close()

        # 期望值取自 PostgreSQL 的级别名（服务端口径，非被测实现）：
        assert override_level == "repeatable read"
        assert default_level == "read committed"

    async def test_repeatable_read_session_keeps_one_snapshot(self, db_engine: AsyncEngine) -> None:
        parent_id = await _insert_message_full(db_engine, created_at=_NEW)
        new_reply_id = uuid.uuid4()

        reader = session_factory(isolation_level="REPEATABLE READ")()
        try:
            # 这一次读建立快照。
            first = await message_row(reader, parent_id)
            assert first is not None

            # 另一会话（引擎默认隔离级别）改这一行、再插一行回复，并已提交。
            async with db_engine.begin() as conn:
                await conn.execute(
                    sa.update(Message).where(Message.id == parent_id).values(subject="另一会话改写")
                )
                await conn.execute(
                    sa.insert(Message).values(
                        id=new_reply_id,
                        direction="inbound",
                        status=RECEIVED_STATE,
                        parent_id=parent_id,
                        recipient="reply@example.com",
                        subject="并发到达的回复",
                        body="并发正文",
                        mailbox="reply@example.com",
                        uidvalidity=999,
                        uid=4242,
                        created_at=_NEW,
                        received_at=_NEW,
                    )
                )

            # 同会话再读：快照未变。`expire_all` 是必须的——否则身份图会把旧对象直接还回来，
            # 这条断言在任何隔离级别下都会绿，就证明不了快照（docs/testing.md §3.6）。
            reader.expire_all()
            again = await message_row(reader, parent_id)
            assert again is not None
            assert again.subject == "夹具主题（无真实收件人）"
            invisible = await replies_page(reader, parent_id, after=(None, None), limit=10)
            assert invisible == []
        finally:
            await reader.close()

        # 快照之外（新会话）两处写入都可见——证明它们真的提交了，上面不是「压根没写进去」。
        fresh = AsyncSession(bind=db_engine, expire_on_commit=False)
        try:
            latest = await message_row(fresh, parent_id)
            visible = await replies_page(fresh, parent_id, after=(None, None), limit=10)
        finally:
            await fresh.close()
        assert latest is not None
        assert latest.subject == "另一会话改写"
        assert [row.id for row in visible] == [new_reply_id]
