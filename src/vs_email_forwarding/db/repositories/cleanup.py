#!/usr/bin/env python3
"""保留期清理仓储：分批 + SKIP LOCKED + NOT EXISTS（spec 0001 / T-10）。

写法口径（真源：`specs/0001-email-forwarding-service/design.md` §2.5、
`plan.md` §5.5、`AGENTS.md` §8.1）：

* **判据与范围**：按各行的 ``created_at``（本行产生时刻）判定到期；
  范围只覆盖 ``messages`` 与 ``rejected_messages``——``pending_messages`` 是
  纯队列、不参与清理。到期时刻（cutoff）由流程层按配置的保留期算出后绑定
  进来，批次大小 batch_size 是入参（``BATCH_SIZE`` 配置项，不写死字面量）。
* **并发口径**：每批先 ``select ... with_for_update(skip_locked=True)`` 锁定
  到期行，再在同一事务内按主键删除；与流② 正在投递的行相撞时被跳过的行
  留到下一轮，删 0 行按幂等忽略。
* **NOT EXISTS 保护**：删出站行必须排除仍有入站待审行指向的行——否则父行
  先删会让流④ 写回复行撞 ``parent_id`` 外键、把正常回复误报成拒审
  （``AC-22``）。回复行不单独删，由外键 ``ON DELETE CASCADE`` 级联清除。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from vs_email_forwarding.db.models import Message, PendingMessage, RejectedMessage

__all__ = [
    "cleanup_expired_messages",
    "cleanup_expired_rejected",
    "expired_messages_statement",
]


def expired_messages_statement(*, cutoff: datetime, batch_size: int) -> Select:
    """构造到期出站行的批量选取：锁行 + 到期谓词 + NOT EXISTS 保护 + 批次上限。

    ``NOT EXISTS`` 排除仍有入站待审行指向的出站行；排序按 ``created_at``
    保证批次公平推进。
    """
    return (
        sa.select(Message.id)
        .where(
            Message.created_at <= cutoff,
            ~sa.exists(sa.select(PendingMessage.id).where(PendingMessage.parent_id == Message.id)),
        )
        .order_by(Message.created_at)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )


async def cleanup_expired_messages(
    session: AsyncSession, *, cutoff: datetime, batch_size: int
) -> int:
    """清理一批到期的邮件行（含经外键级联的回复行），返回删除的行数。

    锁行与删除在同一事务内完成；被并发执行体锁走的行本轮跳过（SKIP LOCKED），
    删 0 行按幂等忽略。到期时刻由 cutoff 承载（流程层按保留期配置合成）。
    """
    ids = list(
        (
            await session.scalars(expired_messages_statement(cutoff=cutoff, batch_size=batch_size))
        ).all()
    )
    if not ids:
        return 0
    result = cast(
        CursorResult[Any],
        await session.execute(sa.delete(Message).where(Message.id.in_(ids))),
    )
    return result.rowcount


async def cleanup_expired_rejected(
    session: AsyncSession, *, cutoff: datetime, batch_size: int
) -> int:
    """清理一批到期的拒审行，返回删除的行数。拒审表无子行、无保护谓词。"""
    ids = list(
        (
            await session.scalars(
                sa.select(RejectedMessage.id)
                .where(RejectedMessage.created_at <= cutoff)
                .order_by(RejectedMessage.created_at)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    if not ids:
        return 0
    result = cast(
        CursorResult[Any],
        await session.execute(sa.delete(RejectedMessage).where(RejectedMessage.id.in_(ids))),
    )
    return result.rowcount
