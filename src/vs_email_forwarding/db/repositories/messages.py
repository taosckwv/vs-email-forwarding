#!/usr/bin/env python3
"""``messages`` 仓储（发信侧）：认领与条件 UPDATE（spec 0001 / T-7）。

写法口径（真源：`specs/0001-email-forwarding-service/design.md` §3 流②、
`plan.md` §5.1 / §5.2）：

* **认领（阶段①）**：``select(...).with_for_update(skip_locked=True)``，
  谓词 ``direction = outbound`` 且 ``status = sending`` 且 ``attempt_count <= 4``
  且 ``next_attempt_at <= <绑定的 now 参数>``；认领第 n 次（n < 4）在同一事务内
  推 ``attempt_count`` 与 ``next_attempt_at``（退避表在 constants）；
  ``attempt_count = 4`` 的行是收割支命中面，认领时**不递增、不推退避**
  （不产生第 5 次投递）。
* **结果（阶段③）**：条件 UPDATE 只允许 ``design.md`` §2.3 声明的三个起点——
  ``sending → sent``、``sending → failed``、``sent → failed``（退信覆写）；
  谓词把起点写进 ``WHERE``，``rowcount = 0`` 按幂等忽略、返回 ``False``。
* 取时一律用流程层绑定进来的 ``now`` 参数，不用 ``func.now()``（``plan.md`` §5.2）。

本模块只含语句构造与执行，不承载业务判定（返回码分类属 ``core/retry.py``）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Final, cast

import sqlalchemy as sa
from sqlalchemy import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from vs_email_forwarding.constants import (
    DIRECTIONS,
    MAX_ATTEMPTS,
    MAX_CLAIMABLE_ATTEMPT_COUNT,
    MESSAGE_STATES,
    RETRY_BACKOFF_SECONDS,
)
from vs_email_forwarding.db.models import Message, PendingMessage

# 受控取值一律从 constants 引用，不在本模块重复字面量：
# 方向取集合首项（DIRECTIONS 即 (outbound, inbound)）；
# 出站三态依次是六态的第 4 ~ 6 项（sending / sent / failed）。
_OUTBOUND: Final = DIRECTIONS[0]
_SENDING: Final = MESSAGE_STATES[3]
_SENT: Final = MESSAGE_STATES[4]
_FAILED: Final = MESSAGE_STATES[5]

__all__ = [
    "claim_due_send_messages",
    "claim_due_send_statement",
    "mark_failed_from_sent",
    "mark_send_failed",
    "mark_sent",
    "message_status",
    "pending_attempt_count",
    "replies_page",
]


def claim_due_send_statement(*, now: datetime, batch_size: int) -> Select:
    """构造发信认领 SELECT：锁行 + 谓词 + 到点排序 + 分批上限。

    谓词即流② 的认领条件（``design.md`` §3 流②）：出站、``status = sending``
    （显式等待态）、``attempt_count <= 4``（放宽到 4 是收割支的命中面）、
    ``next_attempt_at <= now``。``FOR UPDATE SKIP LOCKED`` 保证并发执行体
    各抢各的行（``AC-29`` 的库内侧）。
    """
    return (
        sa.select(Message)
        .where(
            Message.direction == _OUTBOUND,
            Message.status == _SENDING,
            Message.attempt_count <= MAX_CLAIMABLE_ATTEMPT_COUNT,
            Message.next_attempt_at <= now,
        )
        .order_by(Message.next_attempt_at)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )


async def claim_due_send_messages(
    session: AsyncSession, *, now: datetime, batch_size: int
) -> list[Message]:
    """认领一批到点的待发行：锁行、推进尝试计数与退避，提交由调用方负责。

    认领第 n 次（n < 4）把 ``attempt_count`` 推到 n 并按退避表推 ``next_attempt_at``
    （第 4 次不推——该行只剩终态一条路）；``attempt_count = 4`` 的行原样返回，
    由收割支直接推进终态（`plan.md` §5.2 / §5.3）。锁与推进在同一事务内完成，
    不是「先 SELECT 判断、再 UPDATE」的两步写法。
    """
    rows = list(
        (await session.scalars(claim_due_send_statement(now=now, batch_size=batch_size))).all()
    )
    for row in rows:
        if row.attempt_count >= MAX_ATTEMPTS:
            # 收割支命中面：不递增、不推退避、不发起任何外部调用（design.md §3）。
            continue
        row.attempt_count += 1
        backoff_seconds = RETRY_BACKOFF_SECONDS.get(row.attempt_count)
        if backoff_seconds is not None:
            row.next_attempt_at = now + timedelta(seconds=backoff_seconds)
    return rows


async def mark_sent(session: AsyncSession, message_id: uuid.UUID, *, now: datetime) -> bool:
    """条件 UPDATE ``sending → sent``：写 ``sent_at``；rowcount = 0 按幂等忽略。

    谓词把声明的起点写进 ``WHERE``：起点已不是 ``sending``（例：结果重复提交）
    时不写任何行，返回 ``False``，不报错（`plan.md` §5.2）。
    """
    result = cast(
        CursorResult[Any],
        await session.execute(
            sa.update(Message)
            .where(Message.id == message_id, Message.status == _SENDING)
            .values(status=_SENT, sent_at=now)
        ),
    )
    return result.rowcount > 0


async def mark_send_failed(
    session: AsyncSession,
    message_id: uuid.UUID,
    *,
    error_class: str,
    smtp_code: int | None,
    now: datetime,
) -> bool:
    """条件 UPDATE ``sending → failed``：记录最后一次错误的分类与 SMTP 返回码。

    适用于投递失败置终态（含 ``5xx`` 直接终态与收割支推进——第 4 次认领的行
    仍处于 ``sending``，本语句就是它的终态出口）。起点不是 ``sending`` 时不写行。
    ``smtp_code`` 为 ``None`` 表示非 SMTP 故障（如连接超时、收割支无返回码）。
    """
    result = cast(
        CursorResult[Any],
        await session.execute(
            sa.update(Message)
            .where(Message.id == message_id, Message.status == _SENDING)
            .values(status=_FAILED, last_error_class=error_class, last_smtp_code=smtp_code)
        ),
    )
    return result.rowcount > 0


async def mark_failed_from_sent(
    session: AsyncSession,
    message_id: uuid.UUID,
    *,
    error_class: str,
    now: datetime,
) -> bool:
    """条件 UPDATE ``sent → failed``：退信（DSN）把已发出的行覆写为终态失败。

    这是 ``design.md`` §2.3 声明的第三个迁移；它**不写** ``sent_at``（已发出的
    事实不因退信而消失），也不碰 ``attempt_count`` / ``next_attempt_at``
    （结果事务一律不碰这两列，`plan.md` §5.2）。
    """
    result = cast(
        CursorResult[Any],
        await session.execute(
            sa.update(Message)
            .where(Message.id == message_id, Message.status == _SENT)
            .values(status=_FAILED, last_error_class=error_class)
        ),
    )
    return result.rowcount > 0


# ---------------------------------------------------------------------------
# 查询侧（spec 0001 / T-11）：六态判定的库内输入与回复 keyset 分页
# （design §4.1：排序固定 received_at ASC, id ASC；不用 limit/offset）
# ---------------------------------------------------------------------------


async def message_status(session: AsyncSession, message_id: uuid.UUID) -> str | None:
    """读 ``messages.status`` 列值；行不存在返回 ``None``。

    六态中的 ``sending`` / ``sent`` / ``failed`` 由该列值直接表达；
    ``submitted`` / ``reviewing`` / ``rejected`` 的判定输入分别由
    :func:`pending_attempt_count` 与 ``rejected_messages`` 的存在性查询提供，
    优先级判定属 ``core``（``design.md`` §2.3），不在本层。
    """
    row = (await session.execute(sa.select(Message.status).where(Message.id == message_id))).first()
    return None if row is None else row[0]


async def pending_attempt_count(session: AsyncSession, pending_id: uuid.UUID) -> int | None:
    """读待审行的 ``attempt_count``；行不存在返回 ``None``。

    ``0`` 与 ``> 0`` 分别是「已提交」与「审查中」的库内输入（``design.md`` §2.3）。
    """
    row = (
        await session.execute(
            sa.select(PendingMessage.attempt_count).where(PendingMessage.id == pending_id)
        )
    ).first()
    return None if row is None else row[0]


async def replies_page(
    session: AsyncSession,
    parent_id: uuid.UUID,
    *,
    after: tuple[datetime | None, uuid.UUID | None],
    limit: int,
) -> list[sa.Row]:
    """取某出站邮件的一页回复：keyset 游标 ``received_at ASC, id ASC``。

    ``after`` 是上一页最后一行的 ``(received_at, id)``（首页传 ``(None, None)``）；
    排序键与 ``(parent_id, received_at)`` 索引对齐，翻页用行值比较而非
    ``limit`` / ``offset``——新回复到达不会让偏移窗口漂移（``AC-41``）。
    返回 ``(id, received_at)`` 行序列，正好够客户端拼下一页游标。
    """
    after_received_at, after_id = after
    stmt = sa.select(Message.id, Message.received_at).where(Message.parent_id == parent_id)
    if after_received_at is not None and after_id is not None:
        stmt = stmt.where(
            sa.tuple_(Message.received_at, Message.id) > sa.tuple_(after_received_at, after_id)
        )
    stmt = stmt.order_by(Message.received_at, Message.id).limit(limit)
    return list((await session.execute(stmt)).all())
