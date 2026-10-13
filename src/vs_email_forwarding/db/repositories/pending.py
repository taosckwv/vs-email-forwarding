#!/usr/bin/env python3
"""``pending_messages`` 仓储：三阶段认领与条件写入（spec 0001 / T-6）。

写法口径（真源：`specs/0001-email-forwarding-service/design.md` §3、
`plan.md` §2.9 / §5.1 ~ §5.3）：

* **认领（阶段①）**：``select(...).with_for_update(skip_locked=True)``，
  谓词 ``attempt_count <= 4`` 且 ``next_attempt_at <= <绑定的 now 参数>``；
  认领第 n 次（n < 4）在同一事务内把 ``attempt_count`` 推到 n、
  ``next_attempt_at`` 推到 ``now + 退避[n]``；``attempt_count = 4`` 的行
  是收割支的命中面，认领时**不递增、不推退避**（不产生第 5 次尝试）。
* **结果（阶段③）**：过审 → ``messages`` 一行 ``sending``（跨表 ID 同值、
  ``attempt_count`` 重置为 0、``next_attempt_at`` 重置为参数绑定的 now）；
  拒审 / 收割 → ``rejected_messages`` 一行（``source`` 由调用方给：
  ``review_rejected`` / ``review_error`` / ``review_incomplete``）。
  两个目标语句都带 ``ON CONFLICT DO NOTHING``——撞键按「这一封已处理过」幂等忽略，
  之后一律删待审行。
* 取时一律用流程层绑定进来的 ``now`` 参数，不用 ``func.now()``（``plan.md`` §5.2）。

本模块只含语句构造与执行，不承载业务判定（过审与否属审查流）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import Insert as PGInsert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from vs_email_forwarding.constants import (
    DIRECTIONS,
    MAX_ATTEMPTS,
    MAX_CLAIMABLE_ATTEMPT_COUNT,
    MESSAGE_STATES,
    REJECT_SOURCES,
    RETRY_BACKOFF_SECONDS,
)
from vs_email_forwarding.db.models import Message, PendingMessage, RejectedMessage

# 受控取值一律从 constants 引用，不在本模块重复字面量：
# 入站取方向集合第二项（DIRECTIONS 即 (outbound, inbound)），
# sending 是出站六态中 messages.status 的第一个取值。
_INBOUND: Final = DIRECTIONS[1]
_SENDING: Final = MESSAGE_STATES[3]

__all__ = [
    "approval_insert_statement",
    "approve_pending_message",
    "claim_due_pending_messages",
    "claim_due_statement",
    "harvest_pending_message",
    "reject_pending_message",
]


def claim_due_statement(*, direction: str, now: datetime, batch_size: int) -> Select:
    """构造待审行的认领 SELECT：锁行 + 谓词 + 到点排序 + 分批上限。

    谓词即认领条件（``design.md`` §3 阶段①）：方向匹配、``attempt_count <= 4``
    （放宽到 4 是收割支的命中面）、``next_attempt_at <= now``（now 由流程层绑定，
    不用数据库时钟）。``FOR UPDATE SKIP LOCKED`` 保证并发执行体各抢各的行。
    """
    return (
        sa.select(PendingMessage)
        .where(
            PendingMessage.direction == direction,
            PendingMessage.attempt_count <= MAX_CLAIMABLE_ATTEMPT_COUNT,
            PendingMessage.next_attempt_at <= now,
        )
        .order_by(PendingMessage.next_attempt_at)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )


async def claim_due_pending_messages(
    session: AsyncSession, *, direction: str, now: datetime, batch_size: int
) -> list[PendingMessage]:
    """认领一批到点的待审行：锁行、推进尝试计数与退避，提交由调用方负责。

    认领第 n 次（n < 4）把 ``attempt_count`` 推到 n 并按退避表推 ``next_attempt_at``
    （第 4 次不推——该行只剩终态一条路）；``attempt_count = 4`` 的行原样返回，
    由收割支直接推进终态（`plan.md` §5.2 / §5.3）。锁与推进在同一事务内完成，
    不是「先 SELECT 判断、再 UPDATE」的两步写法。
    """
    rows = list(
        (
            await session.scalars(
                claim_due_statement(direction=direction, now=now, batch_size=batch_size)
            )
        ).all()
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


def approval_insert_statement() -> PGInsert:
    """构造过审落库的 INSERT：跨表 ID 同值、重置尝试列、撞键幂等忽略。

    ``attempt_count = 0`` 与 ``next_attempt_at = now`` 建行即重置
    （审查重试不侵占发信重试额度，``design.md`` §2.4）；``ON CONFLICT DO NOTHING``
    冲突目标是主键 ``id``（跨表搬运 ID 同值，撞它即「这一封已处理过」，
    ``plan.md`` §2.9 的构造器写法）。语句的取值在执行时经
    :func:`approve_pending_message` 绑定。
    """
    return pg_insert(Message).on_conflict_do_nothing(index_elements=[Message.id])


async def approve_pending_message(
    session: AsyncSession, pending_id: uuid.UUID, *, now: datetime
) -> bool:
    """过审落库（阶段③）：写 ``messages`` 一行 ``sending`` + 删待审行。

    同一事务内「写目标行 → 删待审行」（`design.md` §3 流① 落库口径）；
    ``created_at`` 原样搬运、保持原始提交时刻，``reviewed_at`` 记绑定的 now。
    撞唯一键 / 主键时只删待审行、不重复写目标行，返回 ``True`` 表示待审行已收尾。
    待审行不存在（已被其他执行体收尾）时返回 ``False``，按幂等忽略。
    """
    row = await session.get(PendingMessage, pending_id)
    if row is None:
        return False
    await session.execute(
        approval_insert_statement().values(
            id=row.id,
            direction=row.direction,
            status=_SENDING,
            message_id=row.message_id,
            recipient=row.recipient,
            subject=row.subject,
            body=row.body,
            body_format=row.body_format,
            attempt_count=0,
            next_attempt_at=now,
            created_at=row.created_at,
            reviewed_at=now,
        )
    )
    await session.execute(sa.delete(PendingMessage).where(PendingMessage.id == pending_id))
    return True


async def reject_pending_message(
    session: AsyncSession,
    pending_id: uuid.UUID,
    *,
    source: str,
    now: datetime,
) -> bool:
    """拒审落库（阶段③）：写 ``rejected_messages`` 一行 + 删待审行。

    跨表 ID 同值（架构图的「id 与目标行同值」）；``subject`` / ``body`` 仅入站行搬运，
    出站行保持 NULL（表上的 ``CHECK`` 兜底）。``source`` 只接受三个受控取值。
    """
    if source not in REJECT_SOURCES:
        msg = f"unknown reject source: {source}"
        raise ValueError(msg)
    row = await session.get(PendingMessage, pending_id)
    if row is None:
        return False
    await session.execute(
        pg_insert(RejectedMessage)
        .on_conflict_do_nothing(index_elements=[RejectedMessage.id])
        .values(
            id=row.id,
            direction=row.direction,
            source=source,
            subject=row.subject if row.direction == _INBOUND else None,
            body=row.body if row.direction == _INBOUND else None,
            last_error_class=row.last_error_class,
            created_at=row.created_at,
            reviewed_at=now,
        )
    )
    await session.execute(sa.delete(PendingMessage).where(PendingMessage.id == pending_id))
    return True


async def harvest_pending_message(
    session: AsyncSession, pending_id: uuid.UUID, *, now: datetime
) -> bool:
    """收割支写入（``plan.md`` §5.3）：第 4 次认领后崩溃的遗留行推进终态。

    等价于 ``source = review_incomplete`` 的拒审落库——不递增计数、不推退避、
    不发起任何外部调用，单独成函数是为了让调用方（审查流）的收割分支
    显式可见、可审计。
    """
    return await reject_pending_message(session, pending_id, source=REJECT_SOURCES[2], now=now)
