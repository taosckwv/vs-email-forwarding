#!/usr/bin/env python3
"""入站幂等入库仓储（spec 0001 / T-12，`FR-7` / `AC-11`）。

两处入站唯一键（真源：`design.md` §2.2 索引表、§3.4 流③ 落库、§3.5 流④ 落库）：

* ``pending_messages`` 与 ``messages`` 各有一份 ``(mailbox, uidvalidity, uid)``
  **部分唯一索引**（``WHERE direction = 'inbound'``）。两处都建的语义：
  重复的入站邮件在流③ 落待审行时即被挡住；万一漏过（例：上次过审删行后
  游标回退又重拉出一行待审），流④ 写 ``received`` 行时仍被 ``messages``
  侧的键兜住——同一封回复在两张表里都只有一条。
* **幂等插入**：``pg_insert(...).on_conflict_do_nothing(index_elements=[三列键],
  index_where=<direction = inbound>)``——推断部分唯一索引必须携带同一 WHERE
  谓词；撞键后 ``rowcount = 0`` 一律按「这一封已处理过」幂等忽略，返回
  ``False``，不依赖异常文本匹配（`plan.md` §2.9）。跨表搬运 ID 同值的重放
  三列必同值，故冲突目标只需声明三列入站键，主键重放场景已被它覆盖。
* 取时一律用流程层绑定进来的 ``now`` 参数，不用 ``func.now()``（`plan.md` §5.2）：
  ``created_at`` 是该回复的落库时刻（§2.5），流④ 搬运时**原样搬运**、不得重新
  取 now 覆盖；``received_at`` / ``reviewed_at`` 在写回复行时取绑定的 now。

本模块只含语句构造与执行，不承载业务判定（分拣去重计数属流③ 编排，T-25）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final, cast

import sqlalchemy as sa
from sqlalchemy import CursorResult
from sqlalchemy.dialects.postgresql import Insert as PGInsert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from vs_email_forwarding.constants import DIRECTIONS, RECEIVED_STATE
from vs_email_forwarding.db.models import Message, PendingMessage

# 受控取值一律从 constants 引用：入站方向取集合第二项（(outbound, inbound)）。
_INBOUND: Final = DIRECTIONS[1]

__all__ = [
    "approve_inbound_pending",
    "inbound_pending_insert_statement",
    "inbound_received_insert_statement",
    "insert_inbound_pending",
]


def inbound_pending_insert_statement() -> PGInsert:
    """构造流③ 入库的幂等 INSERT：撞三列入站唯一键按幂等忽略。

    冲突目标是 ``(mailbox, uidvalidity, uid)`` 部分唯一索引，推断部分索引
    必须携带同一 ``WHERE direction = 'inbound'`` 谓词（`design.md` §2.2）。
    """
    return pg_insert(PendingMessage).on_conflict_do_nothing(
        index_elements=[PendingMessage.mailbox, PendingMessage.uidvalidity, PendingMessage.uid],
        index_where=PendingMessage.direction == _INBOUND,
    )


def inbound_received_insert_statement() -> PGInsert:
    """构造流④ 过审落库的幂等 INSERT：撞三列入站唯一键按幂等忽略。

    ``messages`` 侧同键同谓词：过审删行后游标回退重拉的重复由它兜底
    （`design.md` §2.2 说明：两处都建，重复在落待审行时即被拒）。
    """
    return pg_insert(Message).on_conflict_do_nothing(
        index_elements=[Message.mailbox, Message.uidvalidity, Message.uid],
        index_where=Message.direction == _INBOUND,
    )


async def insert_inbound_pending(
    session: AsyncSession,
    *,
    recipient: str,
    subject: str,
    body: str,
    body_format: str,
    parent_id: uuid.UUID,
    mailbox: str,
    uidvalidity: int,
    uid: int,
    now: datetime,
    pending_id: uuid.UUID | None = None,
) -> bool:
    """流③ 入库（写待审表入站行）；撞入站唯一键按幂等忽略。

    返回是否新写入了行：``False`` 即「这一封已处理过」，流③ 据此计入
    ``duplicate``（`AC-39`）。建行即初值：``attempt_count = 0``、
    ``next_attempt_at = now``；``created_at`` 取该回复的落库时刻（§2.5）。
    """
    result = cast(
        CursorResult[Any],
        await session.execute(
            inbound_pending_insert_statement().values(
                id=pending_id or uuid.uuid4(),
                direction=_INBOUND,
                message_id=None,
                recipient=recipient,
                subject=subject,
                body=body,
                body_format=body_format,
                parent_id=parent_id,
                mailbox=mailbox,
                uidvalidity=uidvalidity,
                uid=uid,
                attempt_count=0,
                next_attempt_at=now,
                created_at=now,
            )
        ),
    )
    return result.rowcount > 0


async def approve_inbound_pending(
    session: AsyncSession, pending_id: uuid.UUID, *, now: datetime
) -> bool:
    """流④ 过审落库：写 ``messages`` 一行 ``received`` + 删待审行。

    同一事务内「写目标行 → 删待审行」；``created_at`` 原样搬运（保持该回复的
    落库时刻），``received_at`` / ``reviewed_at`` 取绑定的 now，``attempt_count``
    与 ``next_attempt_at`` 建行即重置（§2.4）。撞 ``messages`` 侧入站唯一键
    （这一封已入库过）时只删待审行、返回 ``True``；待审行不存在返回 ``False``。
    """
    row = await session.get(PendingMessage, pending_id)
    if row is None:
        return False
    await session.execute(
        inbound_received_insert_statement().values(
            id=row.id,
            direction=_INBOUND,
            status=RECEIVED_STATE,
            message_id=None,
            parent_id=row.parent_id,
            recipient=row.recipient,
            subject=row.subject,
            body=row.body,
            body_format=row.body_format,
            mailbox=row.mailbox,
            uidvalidity=row.uidvalidity,
            uid=row.uid,
            attempt_count=0,
            next_attempt_at=now,
            created_at=row.created_at,
            received_at=now,
            reviewed_at=now,
        )
    )
    await session.execute(sa.delete(PendingMessage).where(PendingMessage.id == pending_id))
    return True
