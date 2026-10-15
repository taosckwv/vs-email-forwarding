#!/usr/bin/env python3
"""流① 审查出站的集成用例（spec 0001 / `T-34` / `FR-3` / `AC-6`）。

真源：`design.md` §3.2（流① 的落点表）、§3 的阶段① ~ ③ 与结果口径、§2.4（重试与错误列）、
§4.2（审查接口的两条路径）；`plan.md` §5.2 / §5.3（认领与收割支）、§7.3 的 `AC-6` 落点行；
`AGENTS.md` §8.1 / §8.7（幂等与「4 次尝试、15/30/60 秒」硬约束）。

**期望值不来自 `review_outbound.py`**：4 次尝试与 15/30/60 秒取自 `AGENTS.md` §8.7（同时与
`constants.py` 的具名常量对照），三个 `source` 取值取自 `design.md` §2.1 / §4.2 的受控词表，
状态取值取自 `constants.py` 的具名常量，落库列的搬运口径取自 `design.md` §3.2（`created_at`
原样搬运、目标行记 `reviewed_at`、结果事务不写尝试列）。

用例只 `await` 一次 tick（`plan.md` §7.1：tick 是普通 async 函数，不等 10 秒），时间推进靠把
`next_attempt_at` 置成已到点（§7.2）。替身只出现在 `providers/` 的接口边界：审查器经
`providers/review/factory.py` 替换（`fake_review`）、发信走**生产适配器** `SmtplibSender` 打到
进程内假 SMTP（`smtp_capture`）——数据库与两条流本身都是真的。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vs_email_forwarding.config import Settings, get_settings
from vs_email_forwarding.constants import (
    DIRECTION_INBOUND,
    DIRECTION_OUTBOUND,
    ERROR_CLASS_INTERNAL,
    MESSAGE_STATE_REJECTED,
    MESSAGE_STATE_SENDING,
    MESSAGE_STATE_SENT,
    REJECT_SOURCE_REVIEW_ERROR,
    REJECT_SOURCE_REVIEW_INCOMPLETE,
    REJECT_SOURCE_REVIEW_REJECTED,
)
from vs_email_forwarding.db.models import Message, PendingMessage, RejectedMessage
from vs_email_forwarding.db.repositories.inbound import insert_inbound_pending
from vs_email_forwarding.db.repositories.pending import insert_outbound_pending
from vs_email_forwarding.flows.review_outbound import review_outbound_tick
from vs_email_forwarding.flows.send_email import send_email_tick
from vs_email_forwarding.service.query_message import query_message

pytestmark = pytest.mark.integration

#: 本次 tick 的注入时刻（`plan.md` §7.2：时间推进不靠 sleep）。
_NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)

#: 待审行的落库时刻：**刻意与 `_NOW` 不同**——只有不同值才能验出「`created_at` 原样搬运」而不是
#: 「落库时重新取一次 now」（`design.md` §2.5 / §3.2）。
_SUBMITTED_AT = datetime(2025, 5, 31, 8, 30, 0, tzinfo=UTC)

#: `AGENTS.md` §8.7 的硬约束：首次尝试 + 3 次重试（合计 4 次），退避 15s / 30s / 60s。
_BACKOFF_SECONDS: dict[int, int] = {1: 15, 2: 30, 3: 60}
_MAX_ATTEMPTS = 4

#: 夹具内容一律用 `example.com` 占位（`AGENTS.md` §8 第 4 条、`docs/testing.md` §4.7）。
_RECIPIENT = "colleague@example.com"
_SUBJECT = "出站审查夹具主题"
_BODY = "出站审查夹具正文"

_Factory = async_sessionmaker[AsyncSession]


async def _fetch(factory: _Factory, statement: Any) -> list[dict[str, Any]]:
    """执行一条只读语句并把结果摊成普通映射。

    用例的读路径与写路径分开：写走被测的流与仓储，读走裸 `SELECT`，断言因此不看实现内部状态。
    """
    async with factory() as session:
        rows = (await session.execute(statement)).all()
    return [dict(row._mapping) for row in rows]


async def _seed_outbound_pending(
    factory: _Factory,
    *,
    pending_id: uuid.UUID | None = None,
    attempt_count: int = 0,
    next_attempt_at: datetime = _NOW,
) -> uuid.UUID:
    """放一行出站待审行（走生产建行路径），再按用例需要摆好尝试计数与到点时刻。

    建行取 `_SUBMITTED_AT`，到点时刻另置：`insert_outbound_pending` 会把 `created_at` 与
    `next_attempt_at` 都取传入的 `now`，而本文件要验「搬运 `created_at`」与「认领推退避」两件
    不同的事，两者必须是可区分的两个时刻。
    """
    row_id = pending_id or uuid.uuid4()
    async with factory() as session:
        await insert_outbound_pending(
            session,
            pending_id=row_id,
            message_id=f"<{row_id}@example.com>",
            recipient=_RECIPIENT,
            subject=_SUBJECT,
            body=_BODY,
            body_format="text",
            now=_SUBMITTED_AT,
        )
        await session.execute(
            sa.update(PendingMessage)
            .where(PendingMessage.id == row_id)
            .values(attempt_count=attempt_count, next_attempt_at=next_attempt_at)
        )
        await session.commit()
    return row_id


async def _seed_inbound_pending(
    factory: _Factory, *, uid: int = 1, next_attempt_at: datetime = _NOW
) -> uuid.UUID:
    """放一行**入站**待审行：流① 必须对方向免疫（`design.md` §3.2 的认领谓词）。"""
    row_id = uuid.uuid4()
    async with factory() as session:
        await insert_inbound_pending(
            session,
            pending_id=row_id,
            recipient=_RECIPIENT,
            subject=_SUBJECT,
            body=_BODY,
            body_format="text",
            parent_id=uuid.uuid4(),
            mailbox="INBOX",
            uidvalidity=1,
            uid=uid,
            now=_SUBMITTED_AT,
        )
        await session.execute(
            sa.update(PendingMessage)
            .where(PendingMessage.id == row_id)
            .values(next_attempt_at=next_attempt_at)
        )
        await session.commit()
    return row_id


async def _pending(factory: _Factory, row_id: uuid.UUID) -> dict[str, Any] | None:
    """读回待审行的三个关键列（行不在即返回 `None`）。"""
    rows = await _fetch(
        factory,
        sa.select(
            PendingMessage.attempt_count,
            PendingMessage.next_attempt_at,
            PendingMessage.created_at,
        ).where(PendingMessage.id == row_id),
    )
    return rows[0] if rows else None


async def _rejected(factory: _Factory, row_id: uuid.UUID) -> dict[str, Any] | None:
    """读回拒审表的相应行（`design.md` §2.1 的列口径）。"""
    rows = await _fetch(
        factory,
        sa.select(
            RejectedMessage.id,
            RejectedMessage.direction,
            RejectedMessage.source,
            RejectedMessage.subject,
            RejectedMessage.body,
            RejectedMessage.last_error_class,
            RejectedMessage.created_at,
            RejectedMessage.reviewed_at,
        ).where(RejectedMessage.id == row_id),
    )
    return rows[0] if rows else None


async def _message(factory: _Factory, row_id: uuid.UUID) -> dict[str, Any] | None:
    """读回邮件表的相应行（`design.md` §2.1 的列口径）。"""
    rows = await _fetch(
        factory,
        sa.select(
            Message.id,
            Message.direction,
            Message.status,
            Message.created_at,
            Message.reviewed_at,
            Message.attempt_count,
            Message.next_attempt_at,
        ).where(Message.id == row_id),
    )
    return rows[0] if rows else None


class TestApprovedPath:
    """过审：写 `messages` 一行 `sending` + 删待审行，同一事务（`design.md` §3.2）。"""

    async def test_approved_writes_sending_row_and_clears_pending(self, session_factory: _Factory):
        pending_id = await _seed_outbound_pending(session_factory)
        settings = get_settings()

        result = await review_outbound_tick(session_factory, settings, now=_NOW)

        assert result.approved == 1
        assert (result.rejected, result.errored, result.harvested, result.retried) == (0, 0, 0, 0)

        row = await _message(session_factory, pending_id)
        assert row is not None
        assert row["status"] == MESSAGE_STATE_SENDING  # 过审即待发行（§3.2）
        assert row["direction"] == DIRECTION_OUTBOUND
        assert row["created_at"] == _SUBMITTED_AT  # 原样搬运，不重取 now
        assert row["reviewed_at"] == _NOW  # 目标行记审查结束时刻
        assert row["attempt_count"] == 0  # 建行即重置（§2.4）
        assert row["next_attempt_at"] == _NOW  # 建行即到点，供流② 抢占
        assert await _pending(session_factory, pending_id) is None  # 待审行已删

    async def test_batch_of_two_is_claimed_and_written_in_one_tick(
        self, session_factory: _Factory, settings_override: Any
    ):
        settings = settings_override(BATCH_SIZE=2)
        first = await _seed_outbound_pending(session_factory)
        second = await _seed_outbound_pending(session_factory)

        result = await review_outbound_tick(session_factory, settings, now=_NOW)

        assert result.approved == 2
        remaining = await _fetch(session_factory, sa.select(PendingMessage.id))
        assert remaining == []
        written = await _fetch(session_factory, sa.select(Message.id, Message.status))
        assert {row["id"] for row in written} == {first, second}
        assert {row["status"] for row in written} == {MESSAGE_STATE_SENDING}

    async def test_reviewer_sees_the_public_id_and_the_full_content(
        self, session_factory: _Factory, fake_review: Any
    ):
        pending_id = await _seed_outbound_pending(session_factory)

        await review_outbound_tick(session_factory, get_settings(), now=_NOW)

        assert len(fake_review.reviewed) == 1
        seen = fake_review.reviewed[0]
        # design.md §4.2 的字段表：message_id 是「对外 ID（即主键）」，其余是即将放行的内容。
        assert seen.message_id == pending_id
        assert seen.direction == DIRECTION_OUTBOUND
        assert seen.recipient == _RECIPIENT
        assert seen.subject == _SUBJECT
        assert seen.body == _BODY
        assert seen.body_format == "text"


class TestRejectedPath:
    """判定拒：写 `rejected_messages` 且 `source = review_rejected`（`design.md` §3.2 / §4.2）。"""

    async def test_rejected_row_carries_source_and_clears_pending(
        self, session_factory: _Factory, fake_review: Any
    ):
        pending_id = await _seed_outbound_pending(session_factory)
        fake_review.mode = "reject"

        result = await review_outbound_tick(session_factory, get_settings(), now=_NOW)

        assert result.rejected == 1
        assert (result.approved, result.errored, result.harvested, result.retried) == (0, 0, 0, 0)
        row = await _rejected(session_factory, pending_id)
        assert row is not None
        assert row["source"] == REJECT_SOURCE_REVIEW_REJECTED
        assert row["direction"] == DIRECTION_OUTBOUND
        assert row["created_at"] == _SUBMITTED_AT  # 原样搬运
        assert row["reviewed_at"] == _NOW
        assert row["last_error_class"] is None  # 判定拒不是「环节出错」
        # 出站行不搬运主题与正文：只有入站行才带内容供复核（design.md §2.1 / §3.5）。
        assert row["subject"] is None
        assert row["body"] is None
        assert await _message(session_factory, pending_id) is None  # 不进邮件表
        assert await _pending(session_factory, pending_id) is None


class TestErrorAndRetry:
    """环节出错（异常 / 超时）按固定重试语义推进，用尽后落 `review_error`（§3.2 / §4.2）。"""

    @pytest.mark.parametrize(
        ("attempt_count", "backoff_seconds"),
        [(0, 15), (1, 30), (2, 60)],
    )
    async def test_error_before_exhaustion_keeps_pending_with_backoff(
        self,
        session_factory: _Factory,
        fake_review: Any,
        attempt_count: int,
        backoff_seconds: int,
    ):
        pending_id = await _seed_outbound_pending(session_factory, attempt_count=attempt_count)
        fake_review.mode = "raise"

        result = await review_outbound_tick(session_factory, get_settings(), now=_NOW)

        assert result.retried == 1
        assert (result.approved, result.rejected, result.errored, result.harvested) == (0, 0, 0, 0)
        row = await _pending(session_factory, pending_id)
        assert row is not None  # 未用尽的出错行不落库，留待下一轮
        assert row["attempt_count"] == attempt_count + 1
        assert row["next_attempt_at"] == _NOW + timedelta(seconds=backoff_seconds)
        assert await _rejected(session_factory, pending_id) is None
        assert await _message(session_factory, pending_id) is None

    async def test_fourth_attempt_error_is_terminal_review_error(
        self, session_factory: _Factory, fake_review: Any
    ):
        # 第 4 次尝试（attempt_count 从 3 认领到 4）是真尝试：仍要发起调用，出错即终态。
        pending_id = await _seed_outbound_pending(session_factory, attempt_count=_MAX_ATTEMPTS - 1)
        fake_review.mode = "raise"

        result = await review_outbound_tick(session_factory, get_settings(), now=_NOW)

        assert result.errored == 1
        assert (result.approved, result.rejected, result.harvested, result.retried) == (0, 0, 0, 0)
        assert len(fake_review.reviewed) == 1
        row = await _rejected(session_factory, pending_id)
        assert row is not None
        assert row["source"] == REJECT_SOURCE_REVIEW_ERROR
        # 分类是「服务自身内部故障」，不是对端的拒绝（design.md §2.4）。
        assert row["last_error_class"] == ERROR_CLASS_INTERNAL
        assert row["created_at"] == _SUBMITTED_AT
        assert row["reviewed_at"] == _NOW
        assert await _pending(session_factory, pending_id) is None
        assert await _message(session_factory, pending_id) is None

    async def test_harvest_branch_skips_reviewer_and_marks_review_incomplete(
        self, session_factory: _Factory, fake_review: Any
    ):
        # attempt_count = 4 是「第 4 次尝试已认领、结果事务未提交」的崩溃遗留行（plan.md §5.3）。
        pending_id = await _seed_outbound_pending(session_factory, attempt_count=_MAX_ATTEMPTS)

        result = await review_outbound_tick(session_factory, get_settings(), now=_NOW)

        assert result.harvested == 1
        assert (result.approved, result.rejected, result.errored, result.retried) == (0, 0, 0, 0)
        assert fake_review.reviewed == []  # 收割支不发起任何外部调用
        row = await _rejected(session_factory, pending_id)
        assert row is not None
        assert row["source"] == REJECT_SOURCE_REVIEW_INCOMPLETE
        assert row["last_error_class"] is None  # 崩溃窗口不存在「最后一次错误」
        assert await _pending(session_factory, pending_id) is None


class TestClaimScope:
    """认领谓词：方向匹配、到点、批上限（`design.md` §3.2；批上限必须是配置项）。"""

    async def test_claim_respects_batch_size(
        self, session_factory: _Factory, fake_review: Any, settings_override: Any
    ):
        settings = settings_override(BATCH_SIZE=2)
        await _seed_outbound_pending(session_factory, next_attempt_at=_NOW - timedelta(seconds=3))
        await _seed_outbound_pending(session_factory, next_attempt_at=_NOW - timedelta(seconds=2))
        latest = await _seed_outbound_pending(
            session_factory, next_attempt_at=_NOW - timedelta(seconds=1)
        )

        result = await review_outbound_tick(session_factory, settings, now=_NOW)

        assert result.approved == 2  # 批上限取自配置，不是常量
        remaining = {
            row["id"] for row in await _fetch(session_factory, sa.select(PendingMessage.id))
        }
        # 按到点时刻升序取前 2 条：最晚那条留下且一字未动。
        assert remaining == {latest}
        row = await _pending(session_factory, latest)
        assert row is not None
        assert row["attempt_count"] == 0

    async def test_claim_ignores_pending_rows_of_the_other_direction(
        self, session_factory: _Factory, fake_review: Any, settings_override: Any
    ):
        settings = settings_override(BATCH_SIZE=2)
        # 入站行**到得最早**：认领谓词里少了方向，它就会第一个被流① 抢走（design.md §3.2）。
        inbound_id = await _seed_inbound_pending(
            session_factory, next_attempt_at=_NOW - timedelta(seconds=4)
        )
        first = await _seed_outbound_pending(
            session_factory, next_attempt_at=_NOW - timedelta(seconds=2)
        )
        second = await _seed_outbound_pending(
            session_factory, next_attempt_at=_NOW - timedelta(seconds=1)
        )
        directions = {
            row["id"]: row["direction"]
            for row in await _fetch(
                session_factory, sa.select(PendingMessage.id, PendingMessage.direction)
            )
        }
        # 先确认夹具真的放了三个方向正确的待审行，否则下面两条断言什么都不成立。
        assert directions == {
            inbound_id: DIRECTION_INBOUND,
            first: DIRECTION_OUTBOUND,
            second: DIRECTION_OUTBOUND,
        }

        result = await review_outbound_tick(session_factory, settings, now=_NOW)

        assert result.approved == 2  # 两封出站邮件都过审
        assert {seen.direction for seen in fake_review.reviewed} == {DIRECTION_OUTBOUND}
        remaining = set(
            row["id"] for row in await _fetch(session_factory, sa.select(PendingMessage.id))
        )
        assert remaining == {inbound_id}
        row = await _pending(session_factory, inbound_id)
        assert row is not None
        assert row["attempt_count"] == 0  # 入站行没被出站流碰过

    async def test_not_due_row_is_not_claimed(self, session_factory: _Factory, fake_review: Any):
        pending_id = await _seed_outbound_pending(
            session_factory, next_attempt_at=_NOW + timedelta(seconds=1)
        )

        result = await review_outbound_tick(session_factory, get_settings(), now=_NOW)

        assert (
            result.approved,
            result.rejected,
            result.errored,
            result.harvested,
            result.retried,
        ) == (
            0,
            0,
            0,
            0,
            0,
        )
        assert fake_review.reviewed == []
        row = await _pending(session_factory, pending_id)
        assert row is not None
        assert row["attempt_count"] == 0
        assert row["next_attempt_at"] == _NOW + timedelta(seconds=1)


class TestIdempotency:
    """重放同一封：撞主键按「这一封已经处理过」处理，只删待审行（`design.md` §3.2）。"""

    async def test_replay_after_approval_does_not_duplicate_target_row(
        self, session_factory: _Factory, fake_review: Any
    ):
        pending_id = await _seed_outbound_pending(session_factory)
        await review_outbound_tick(session_factory, get_settings(), now=_NOW)
        first = await _message(session_factory, pending_id)
        assert first is not None

        # 重放：同一 ID 的待审行再次进入队列（跨表 ID 同值 → 目标行已存在）。
        await _seed_outbound_pending(session_factory, pending_id=pending_id)
        result = await review_outbound_tick(session_factory, get_settings(), now=_NOW)

        assert result.approved == 1
        written = await _fetch(session_factory, sa.select(Message.id))
        assert [row["id"] for row in written] == [pending_id]  # 没有第二行
        assert await _pending(session_factory, pending_id) is None  # 待审行照样被删掉
        assert await _message(session_factory, pending_id) == first  # 目标行一字未改


class TestAcceptanceSix:
    """`AC-6`：恒拒的假审查下，该邮件不产生任何 SMTP 投递，且状态为拒审。

    `plan.md` §7.3 给的落点用例名是 `test_reject_no_smtp`；本类另加一条
    `test_approved_then_send_tick_delivers_once` 作为**`AC-6` 负向断言的对照组**——没有它，
    「零投递」就是一条可能恒绿的断言：必须在同一夹具语境下证明这条发信路径确实能投出。
    发送流的深水区（重试/退避/4 次耗尽/5xx 例外/幂等认领）归 `T-35`，此处只做 happy-path 对照。
    """

    async def test_reject_no_smtp(
        self, session_factory: _Factory, fake_review: Any, smtp_capture: Any
    ):
        pending_id = await _seed_outbound_pending(session_factory)
        fake_review.mode = "reject"
        settings = get_settings()

        review_result = await review_outbound_tick(session_factory, settings, now=_NOW)
        send_result = await send_email_tick(session_factory, settings, now=_NOW)

        assert review_result.rejected == 1
        assert smtp_capture.delivery_count == 0  # 不产生任何 SMTP 投递
        assert smtp_capture.connection_count == 0  # 连都没连上，不只是没投出去
        assert (
            send_result.sent,
            send_result.failed,
            send_result.harvested,
            send_result.retried,
        ) == (
            0,
            0,
            0,
            0,
        )
        assert await _fetch(session_factory, sa.select(Message.id)) == []
        status = await query_message(pending_id)
        assert status.found is True
        assert status.status == MESSAGE_STATE_REJECTED  # 「其状态为拒审」（§4.1 的六态判定）
        rejected = await _rejected(session_factory, pending_id)
        assert rejected is not None
        assert rejected["source"] == REJECT_SOURCE_REVIEW_REJECTED

    async def test_approved_then_send_tick_delivers_once(
        self, session_factory: _Factory, fake_review: Any, smtp_capture: Any
    ):
        pending_id = await _seed_outbound_pending(session_factory)
        settings: Settings = get_settings()

        await review_outbound_tick(session_factory, settings, now=_NOW)
        send_result = await send_email_tick(session_factory, settings, now=_NOW)

        # AC-6 负向断言的对照组：同一夹具语境下这条路真的能投出去（见类 docstring 的分工口径）。
        assert send_result.sent == 1
        assert smtp_capture.delivery_count == 1
        status = await query_message(pending_id)
        assert status.status == MESSAGE_STATE_SENT
