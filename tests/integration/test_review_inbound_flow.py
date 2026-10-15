#!/usr/bin/env python3
"""流④ 审查回复的集成用例（spec 0001 / `T-34` / `FR-8` / `AC-12`）。

真源：`design.md` §3.5（流④ 的落点与结果口径）、§3 阶段① ~ ③（与流① 同构）、§2.2（两处入站
唯一键）、§2.5（`created_at` / `received_at` 是**该回复的落库时刻**、跨表原样搬运）、§4.2
（审查接口的两条路径）、§6（`reply_target_gone` 的已否决方向：不把正常回复误报成拒审）；
`plan.md` §5.2 / §5.3、§7.3 的 `AC-12` 落点行；`AGENTS.md` §8.1 / §8.7。

**期望值不来自 `review_inbound.py`**：三态落点与列搬运口径取自 `design.md` §3.5，`source` 与
`status` 取值取自 `constants.py` 的具名常量，重试算术取自 `AGENTS.md` §8.7（4 次尝试、15/30/60
秒）。`AC-12` 的判定用**调用方视角**的服务入口 `service/query_replies.query_replies`（`GET
.../replies` 的用例），而不是直接数表——「不进入可查询到的回复集合」按可查询面判定。

入站待审行由本文件的夹具直接写成（流③ 的取回与分拣属 `test_fetch_inbound_flow.py`，T-36）：
这里验的是**流④ 拿到一行入站待审行之后的推进与幂等**。替身只在 `providers/` 的接口边界：
审查器经 `providers/review/factory.py` 替换（`fake_review`），数据库是真的。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vs_email_forwarding.config import get_settings
from vs_email_forwarding.constants import (
    DIRECTION_INBOUND,
    ERROR_CLASS_INTERNAL,
    MESSAGE_STATE_SENT,
    RECEIVED_STATE,
    REJECT_SOURCE_REVIEW_ERROR,
    REJECT_SOURCE_REVIEW_INCOMPLETE,
    REJECT_SOURCE_REVIEW_REJECTED,
)
from vs_email_forwarding.db.models import Message, PendingMessage, RejectedMessage
from vs_email_forwarding.db.repositories.inbound import insert_inbound_pending
from vs_email_forwarding.db.repositories.pending import insert_outbound_pending
from vs_email_forwarding.flows.review_inbound import review_inbound_tick
from vs_email_forwarding.service.query_replies import query_replies

pytestmark = pytest.mark.integration

#: 本次 tick 的注入时刻（`plan.md` §7.2：时间推进不靠 sleep）。
_NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)

#: 回复在流③ 的落库时刻：**刻意与 `_NOW` 不同**——只有不同值才能验出「原样搬运」而不是
#: 「落库时重新取 now」（`design.md` §2.5 / §3.5：`created_at` 与 `received_at` 都搬运它）。
_REPLY_LOGGED_AT = datetime(2025, 5, 31, 8, 30, 0, tzinfo=UTC)

#: `AGENTS.md` §8.7 的硬约束：首次尝试 + 3 次重试（合计 4 次），退避 15s / 30s / 60s。
_MAX_ATTEMPTS = 4

#: 夹具内容一律用 `example.com` 占位（`AGENTS.md` §8 第 4 条、`docs/testing.md` §4.7）。
_RECIPIENT = "colleague@example.com"
_SUBJECT = "入站审查夹具主题"
_BODY = "入站审查夹具正文"
_MAILBOX = "INBOX"
_UIDVALIDITY = 4242

_Factory = async_sessionmaker[AsyncSession]


async def _fetch(factory: _Factory, statement: Any) -> list[dict[str, Any]]:
    """执行一条只读语句并把结果摊成普通映射（用例的读路径走裸 `SELECT`）。"""
    async with factory() as session:
        rows = (await session.execute(statement)).all()
    return [dict(row._mapping) for row in rows]


async def _seed_parent_outbound(factory: _Factory) -> tuple[uuid.UUID, str]:
    """放一行**已发出**的出站邮件：它是入站回复的父行（`messages` 自引用外键）。"""
    parent_id = uuid.uuid4()
    parent_message_id = f"<{parent_id}@example.com>"
    async with factory() as session:
        await session.execute(
            sa.insert(Message).values(
                id=parent_id,
                direction="outbound",
                status=MESSAGE_STATE_SENT,
                message_id=parent_message_id,
                recipient=_RECIPIENT,
                subject="出站父行主题",
                body="出站父行正文",
                body_format="text",
                attempt_count=0,
                next_attempt_at=_NOW,
                created_at=_NOW,
                sent_at=_NOW,
                reviewed_at=_NOW,
            )
        )
        await session.commit()
    return parent_id, parent_message_id


async def _seed_inbound_pending(
    factory: _Factory,
    *,
    parent_id: uuid.UUID | None,
    pending_id: uuid.UUID | None = None,
    uid: int = 7001,
    attempt_count: int = 0,
    next_attempt_at: datetime = _NOW,
) -> uuid.UUID:
    """放一行入站待审回复（走生产建行路径），再按用例需要摆好尝试计数与到点时刻。

    `parent_id` 允许指向**不存在的行**：待审表这一列没有外键，正是「父行已被保留期清理」这一
    分支的可构造前提（`design.md` §3.5）。
    """
    row_id = pending_id or uuid.uuid4()
    async with factory() as session:
        await insert_inbound_pending(
            session,
            pending_id=row_id,
            recipient=_RECIPIENT,
            subject=_SUBJECT,
            body=_BODY,
            body_format="text",
            parent_id=parent_id if parent_id is not None else uuid.uuid4(),
            mailbox=_MAILBOX,
            uidvalidity=_UIDVALIDITY,
            uid=uid,
            now=_REPLY_LOGGED_AT,
        )
        await session.execute(
            sa.update(PendingMessage)
            .where(PendingMessage.id == row_id)
            .values(attempt_count=attempt_count, next_attempt_at=next_attempt_at)
        )
        await session.commit()
    return row_id


async def _pending(factory: _Factory, row_id: uuid.UUID) -> dict[str, Any] | None:
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
    rows = await _fetch(
        factory,
        sa.select(
            Message.id,
            Message.direction,
            Message.status,
            Message.parent_id,
            Message.mailbox,
            Message.uidvalidity,
            Message.uid,
            Message.created_at,
            Message.received_at,
            Message.reviewed_at,
            Message.attempt_count,
            Message.next_attempt_at,
        ).where(Message.id == row_id),
    )
    return rows[0] if rows else None


class TestApprovedPath:
    """过审：写 `messages` 一行 `received` + 删待审行（`design.md` §3.5）。"""

    async def test_approved_writes_received_row_and_clears_pending(self, session_factory: _Factory):
        parent_id, _ = await _seed_parent_outbound(session_factory)
        pending_id = await _seed_inbound_pending(session_factory, parent_id=parent_id)

        result = await review_inbound_tick(session_factory, get_settings(), now=_NOW)

        assert result.approved == 1
        assert (result.rejected, result.errored, result.harvested, result.retried) == (0, 0, 0, 0)
        assert result.discarded == 0

        row = await _message(session_factory, pending_id)
        assert row is not None
        assert row["status"] == RECEIVED_STATE  # 入站行的唯一终态
        assert row["direction"] == DIRECTION_INBOUND
        assert row["parent_id"] == parent_id
        # §2.5 / §3.5：created_at 与 received_at 都原样搬运该回复的落库时刻，不重取 now。
        assert row["created_at"] == _REPLY_LOGGED_AT
        assert row["received_at"] == _REPLY_LOGGED_AT
        assert row["reviewed_at"] == _NOW
        # 入站幂等键三要素随行搬运（design.md §2.2）。
        assert (row["mailbox"], row["uidvalidity"], row["uid"]) == (_MAILBOX, _UIDVALIDITY, 7001)
        assert row["attempt_count"] == 0  # 建行即重置（§2.4）
        assert row["next_attempt_at"] == _NOW
        assert await _pending(session_factory, pending_id) is None
        assert await _rejected(session_factory, pending_id) is None

    async def test_reviewer_is_called_with_inbound_direction_and_public_id(
        self, session_factory: _Factory, fake_review: Any
    ):
        parent_id, _ = await _seed_parent_outbound(session_factory)
        pending_id = await _seed_inbound_pending(session_factory, parent_id=parent_id)

        await review_inbound_tick(session_factory, get_settings(), now=_NOW)

        assert len(fake_review.reviewed) == 1
        seen = fake_review.reviewed[0]
        # design.md §4.2 的字段表：message_id 是「对外 ID（即主键）」，direction 决定审查策略。
        assert seen.message_id == pending_id
        assert seen.direction == DIRECTION_INBOUND
        assert seen.subject == _SUBJECT
        assert seen.body == _BODY


class TestRejectedPath:
    """判定拒：入站回复落 `rejected_messages`（带内容供复核），**不进可查询的回复集合**。"""

    async def test_rejected_reply_not_queryable(self, session_factory: _Factory, fake_review: Any):
        parent_id, _ = await _seed_parent_outbound(session_factory)
        pending_id = await _seed_inbound_pending(session_factory, parent_id=parent_id)
        fake_review.mode = "reject"

        result = await review_inbound_tick(session_factory, get_settings(), now=_NOW)

        assert result.rejected == 1
        assert (result.approved, result.errored, result.harvested, result.retried) == (0, 0, 0, 0)
        row = await _rejected(session_factory, pending_id)
        assert row is not None
        assert row["source"] == REJECT_SOURCE_REVIEW_REJECTED
        assert row["direction"] == DIRECTION_INBOUND
        # 入站行带上该回复的主题与正文供复核（design.md §2.1 / §3.5 与出站行相反）。
        assert row["subject"] == _SUBJECT
        assert row["body"] == _BODY
        assert row["last_error_class"] is None  # 判定拒不是「环节出错」
        assert row["created_at"] == _REPLY_LOGGED_AT
        assert row["reviewed_at"] == _NOW
        assert await _message(session_factory, pending_id) is None  # 不入邮件表
        assert await _pending(session_factory, pending_id) is None

        # AC-12 的判定面：调用方能查到的回复集合里没有它（父行仍在，故 found 为 true）。
        page = await query_replies(parent_id)
        assert page.found is True
        assert page.replies == []

    async def test_approved_reply_is_queryable(self, session_factory: _Factory, fake_review: Any):
        # 正向对照：同一条查询路径在过审时**会**返回该回复——否则上面那个空集合恒成立。
        parent_id, _ = await _seed_parent_outbound(session_factory)
        pending_id = await _seed_inbound_pending(session_factory, parent_id=parent_id)

        await review_inbound_tick(session_factory, get_settings(), now=_NOW)

        page = await query_replies(parent_id)
        assert page.found is True
        assert [reply.id for reply in page.replies] == [pending_id]
        # 排序键就是流③ 的落库时刻（design.md §2.5），不是审查时刻。
        assert page.replies[0].received_at == _REPLY_LOGGED_AT


class TestParentGone:
    """父行已被保留期清理：按 `reply_target_gone` 丢弃，**不写成拒审**（§3.5 / §6）。"""

    async def test_parent_gone_is_discarded_not_rejected(
        self, session_factory: _Factory, fake_review: Any
    ):
        # 待审行的 parent_id 指向一行不存在的邮件：清理流先删了父行（§3.5 的场景）。
        pending_id = await _seed_inbound_pending(session_factory, parent_id=None)

        result = await review_inbound_tick(session_factory, get_settings(), now=_NOW)

        assert result.discarded == 1
        assert (
            result.approved,
            result.rejected,
            result.errored,
            result.harvested,
            result.retried,
        ) == (0, 0, 0, 0, 0)
        assert await _message(session_factory, pending_id) is None
        # §6 明令否掉的方向：不把用户的正常回复误报成拒审。
        assert await _rejected(session_factory, pending_id) is None
        assert await _fetch(session_factory, sa.select(RejectedMessage.id)) == []
        # 待审行必须被删掉，否则会被反复认领并在第 4 次误报成拒审（§3.6）。
        assert await _pending(session_factory, pending_id) is None


class TestErrorAndRetry:
    """环节出错按固定重试语义推进，用尽后落 `review_error` + `internal`（§3.5 / §4.2）。"""

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
        parent_id, _ = await _seed_parent_outbound(session_factory)
        pending_id = await _seed_inbound_pending(
            session_factory, parent_id=parent_id, attempt_count=attempt_count
        )
        fake_review.mode = "raise"

        result = await review_inbound_tick(session_factory, get_settings(), now=_NOW)

        assert result.retried == 1
        assert (result.approved, result.rejected, result.errored, result.harvested) == (0, 0, 0, 0)
        assert result.discarded == 0
        row = await _pending(session_factory, pending_id)
        assert row is not None  # 未用尽的出错行不落库
        assert row["attempt_count"] == attempt_count + 1
        assert row["next_attempt_at"] == _NOW + timedelta(seconds=backoff_seconds)
        assert await _rejected(session_factory, pending_id) is None
        assert await _message(session_factory, pending_id) is None

    async def test_fourth_attempt_error_is_terminal_review_error(
        self, session_factory: _Factory, fake_review: Any
    ):
        parent_id, _ = await _seed_parent_outbound(session_factory)
        pending_id = await _seed_inbound_pending(
            session_factory, parent_id=parent_id, attempt_count=_MAX_ATTEMPTS - 1
        )
        fake_review.mode = "raise"

        result = await review_inbound_tick(session_factory, get_settings(), now=_NOW)

        assert result.errored == 1
        assert (result.approved, result.rejected, result.harvested, result.retried) == (0, 0, 0, 0)
        assert len(fake_review.reviewed) == 1  # 真·第 4 次尝试仍要发起调用
        row = await _rejected(session_factory, pending_id)
        assert row is not None
        assert row["source"] == REJECT_SOURCE_REVIEW_ERROR
        assert row["last_error_class"] == ERROR_CLASS_INTERNAL
        assert row["subject"] == _SUBJECT  # 入站行同样带内容供复核
        assert row["body"] == _BODY
        assert await _pending(session_factory, pending_id) is None
        assert await _message(session_factory, pending_id) is None

    async def test_harvest_branch_skips_reviewer_and_marks_review_incomplete(
        self, session_factory: _Factory, fake_review: Any
    ):
        parent_id, _ = await _seed_parent_outbound(session_factory)
        pending_id = await _seed_inbound_pending(
            session_factory, parent_id=parent_id, attempt_count=_MAX_ATTEMPTS
        )

        result = await review_inbound_tick(session_factory, get_settings(), now=_NOW)

        assert result.harvested == 1
        assert (result.approved, result.rejected, result.errored, result.retried) == (0, 0, 0, 0)
        assert fake_review.reviewed == []  # 收割支不发起任何外部调用
        row = await _rejected(session_factory, pending_id)
        assert row is not None
        assert row["source"] == REJECT_SOURCE_REVIEW_INCOMPLETE
        assert row["last_error_class"] is None
        assert await _pending(session_factory, pending_id) is None


class TestClaimScope:
    """认领谓词：流④ 只认入站行（`design.md` §3.5 的认领口径与流① 同构）。"""

    async def test_outbound_row_is_not_claimed_by_the_inbound_flow(
        self, session_factory: _Factory, fake_review: Any
    ):
        outbound_id = uuid.uuid4()
        async with session_factory() as session:
            await insert_outbound_pending(
                session,
                pending_id=outbound_id,
                message_id=f"<{outbound_id}@example.com>",
                recipient=_RECIPIENT,
                subject="出站待审行",
                body="出站待审行正文",
                body_format="text",
                now=_NOW,
            )
            await session.commit()

        result = await review_inbound_tick(session_factory, get_settings(), now=_NOW)

        assert (
            result.approved,
            result.rejected,
            result.errored,
            result.harvested,
            result.retried,
            result.discarded,
        ) == (0, 0, 0, 0, 0, 0)
        assert fake_review.reviewed == []
        row = await _pending(session_factory, outbound_id)
        assert row is not None
        assert row["attempt_count"] == 0  # 未认领的行一字未动
        assert await _message(session_factory, outbound_id) is None


class TestIdempotency:
    """两处入站唯一键（`design.md` §2.2）：游标回退重拉同一封，邮件表里仍只有一条。"""

    async def test_replay_of_received_reply_keeps_single_inbound_row(
        self, session_factory: _Factory, fake_review: Any
    ):
        parent_id, _ = await _seed_parent_outbound(session_factory)
        first = await _seed_inbound_pending(session_factory, parent_id=parent_id, uid=8001)
        await review_inbound_tick(session_factory, get_settings(), now=_NOW)
        written = await _message(session_factory, first)
        assert written is not None

        # 重放：同一 (mailbox, uidvalidity, uid) 再次进入待审表（新 ID，模拟游标回退重拉）。
        replayed = await _seed_inbound_pending(session_factory, parent_id=parent_id, uid=8001)
        assert replayed != first
        result = await review_inbound_tick(session_factory, get_settings(), now=_NOW)

        assert result.approved == 1  # 撞入站唯一键按「已在库里」处理，仍算过审收尾
        inbound_rows = await _fetch(
            session_factory,
            sa.select(Message.id).where(Message.direction == DIRECTION_INBOUND),
        )
        assert [row["id"] for row in inbound_rows] == [first]  # 没有第二条
        assert await _message(session_factory, first) == written  # 已入库那行一字未改
        assert await _pending(session_factory, replayed) is None  # 重放的待审行照样收尾
