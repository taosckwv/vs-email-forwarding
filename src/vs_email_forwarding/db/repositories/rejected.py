#!/usr/bin/env python3
"""``rejected_messages`` 仓储：写入与存在性查询（spec 0001 / T-8）。

写法口径（真源：`specs/0001-email-forwarding-service/design.md` §2.1、§3 流①④
落库口径、`plan.md` §2.9）：

* **写入**：``pg_insert(...).on_conflict_do_nothing(index_elements=[主键])``——
  跨表 ID 同值搬运，撞主键（同一条拒审被重复推进）按幂等忽略、返回 ``False``。
  ``source`` 只接受 constants 的三个受控取值；主题与正文**仅入站行**搬运
  （出站行保持 NULL，由表上的 ``CHECK`` 兜底），入库时**不整封原文**。
* **存在性查询**：``select(...).where(id == ...)`` 配 ``sa.exists`` 的等价写法，
  供流④ 在写回复行前判「原邮件是否还在」等场景使用。

本模块只含语句构造与执行，不承载业务判定（拒审与否属审查流）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy import CursorResult
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from vs_email_forwarding.constants import REJECT_SOURCES
from vs_email_forwarding.db.models import RejectedMessage

__all__ = ["insert_rejected_message", "rejected_message_exists"]


async def insert_rejected_message(
    session: AsyncSession,
    *,
    rejected_id: uuid.UUID,
    direction: str,
    source: str,
    created_at: datetime,
    reviewed_at: datetime,
    subject: str | None = None,
    body: str | None = None,
    last_error_class: str | None = None,
) -> bool:
    """写入一行拒审记录；撞主键按幂等忽略，返回是否新写入了行。

    ``source`` 越界（不在 constants 的受控集合内）在写入前即拒绝；
    ``subject`` / ``body`` 只应为入站行携带（出站行传 ``None``，
    ``ck_rejected_messages_inbound_content`` 在库里兜底）。
    """
    if source not in REJECT_SOURCES:
        msg = f"unknown reject source: {source}"
        raise ValueError(msg)
    result = cast(
        CursorResult[Any],
        await session.execute(
            pg_insert(RejectedMessage)
            .on_conflict_do_nothing(index_elements=[RejectedMessage.id])
            .values(
                id=rejected_id,
                direction=direction,
                source=source,
                subject=subject,
                body=body,
                last_error_class=last_error_class,
                created_at=created_at,
                reviewed_at=reviewed_at,
            )
        ),
    )
    return result.rowcount > 0


async def rejected_message_exists(session: AsyncSession, rejected_id: uuid.UUID) -> bool:
    """按 ID 查询拒审行是否存在（不改任何状态）。"""
    stmt = sa.select(RejectedMessage.id).where(RejectedMessage.id == rejected_id)
    return (await session.execute(stmt)).first() is not None
