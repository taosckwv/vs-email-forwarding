#!/usr/bin/env python3
"""``imap_cursors`` 仓储：读取、初始化、重置与同事务推进（spec 0001 / T-9）。

写法口径（真源：`specs/0001-email-forwarding-service/design.md` §2.2、
`plan.md` §5.4 流③、§2.9）：

* **初始化**：首见信箱建一行，``last_uid = UIDNEXT - 1``（UID 从 1 起算的闭区间，
  「下封从 UIDNEXT 开始」即「已读到 UIDNEXT - 1」）。
* **UIDVALIDITY 变化即重置**：``on_conflict_do_update`` 的 ``set_`` 带
  ``where uidvalidity != <新值>`` 谓词——信箱被整体替换时进度作废、回到
  ``UIDNEXT - 1``；同一 UIDVALIDITY 的例行 ensure **不重写** ``last_uid``。
* **同事务推进**：流③ 每落库一封就在**同一事务**里把 ``last_uid`` 推到该封的
  UID；更新是单调的（``where last_uid < <uid>`` 且 UIDVALIDITY 匹配），
  回退尝试与外库 UID 一律 ``rowcount = 0`` 按幂等忽略。撞唯一键的落库被
  ``on_conflict_do_nothing`` 吞掉后，本事务内的推进照常提交——
  「冲突不得把游标推进一起回滚」（``AC-11`` / ``AC-39``）由此成立。

取时一律用流程层绑定进来的 ``now`` 参数（``updated_at``），不用 ``func.now()``。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy import CursorResult
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from vs_email_forwarding.db.models import ImapCursor

__all__ = ["advance_cursor", "ensure_cursor", "read_cursor"]


async def read_cursor(session: AsyncSession, mailbox: str) -> tuple[int, int] | None:
    """读取游标：返回 ``(uidvalidity, last_uid)``；尚无该信箱的行时返回 ``None``。"""
    row = (
        await session.execute(
            sa.select(ImapCursor.uidvalidity, ImapCursor.last_uid).where(
                ImapCursor.mailbox == mailbox
            )
        )
    ).first()
    return None if row is None else (row[0], row[1])


async def ensure_cursor(
    session: AsyncSession,
    mailbox: str,
    *,
    uidvalidity: int,
    uidnext: int,
    now: datetime,
) -> tuple[int, int]:
    """读取或初始化游标；UIDVALIDITY 变化时重置 ``last_uid``。

    首见信箱建行（``last_uid = UIDNEXT - 1``）；已有行且 UIDVALIDITY 未变时不重写；
    UIDVALIDITY 变了（信箱被整体替换）则把进度重置回新 ``UIDNEXT - 1``。
    返回 ensure 后的 ``(uidvalidity, last_uid)``，调用方据此确定拉取起点。
    """
    initial_last_uid = uidnext - 1
    stmt = (
        pg_insert(ImapCursor)
        .values(
            mailbox=mailbox,
            uidvalidity=uidvalidity,
            last_uid=initial_last_uid,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[ImapCursor.mailbox],
            # 只有 UIDVALIDITY 变化才重置；同一 UIDVALIDITY 的例行 ensure 不动进度。
            set_={"uidvalidity": uidvalidity, "last_uid": initial_last_uid, "updated_at": now},
            where=ImapCursor.uidvalidity != uidvalidity,
        )
        .returning(ImapCursor.uidvalidity, ImapCursor.last_uid)
    )
    row = (await session.execute(stmt)).first()
    if row is not None:
        return (row[0], row[1])
    # 未触发更新（UIDVALIDITY 未变的例行 ensure）：读回现有进度。
    existing = await read_cursor(session, mailbox)
    assert existing is not None  # 冲突分支存在的前提是行已存在
    return existing


async def advance_cursor(
    session: AsyncSession,
    mailbox: str,
    *,
    uidvalidity: int,
    uid: int,
    now: datetime,
) -> bool:
    """把 ``last_uid`` 单调推进到 ``uid``；与分拣落库共用调用方的事务。

    谓词同时要求 UIDVALIDITY 匹配与 ``last_uid < uid``（只前进、不回退）；
    ``rowcount = 0``（旧 UID、外库、或无行）按幂等忽略并返回 ``False``。
    """
    result = cast(
        CursorResult[Any],
        await session.execute(
            sa.update(ImapCursor)
            .where(
                ImapCursor.mailbox == mailbox,
                ImapCursor.uidvalidity == uidvalidity,
                ImapCursor.last_uid < uid,
            )
            .values(last_uid=uid, updated_at=now)
        ),
    )
    return result.rowcount > 0
