#!/usr/bin/env python3
"""流⑤ 保留期清理：分批删除到期的邮件行与拒审行（spec 0001 / T-22）。

设计真源：`specs/0001-email-forwarding-service/design.md` §3.6（保留期清理）、
`plan.md` §5.5（并发口径）、`spec.md` 的 `NFR-8`（保留 30 天）。

口径：

* **范围**只覆盖 ``messages`` 与 ``rejected_messages``——``pending_messages`` 是纯队列、
  不参与清理；回复行由 ``parent_id`` 外键级联清除，不单独删（`design.md` §3.6）。
* **分批**：每批按 ``BATCH_SIZE``（配置项 `Settings.batch_size`，不写字面量）取行并
  ``SELECT ... FOR UPDATE SKIP LOCKED`` 后删除。一批删满说明还有到期行，继续下一批；
  某一批一条也删不掉时停止——被流② 锁住的行本轮跳过、留给下一轮（`plan.md` §5.5）。
* **到期时刻（cutoff）由调用方给出**：本模块只管编排，保留期时长与取时点属于调用方
  （它不在 `design.md` §7 的配置项清单里，本模块因此不自造一个默认值）。
  ``now`` 可注入，好让集成用例不依赖墙钟（`plan.md` §7.2 的时间推进口径）。
* 单次 tick 就是一个**普通的 async 函数**，测试里 ``await`` 一次即可（`AC-22` 的
  「执行一次清理动作」；可测性结论见 `docs/research/02-runtime-and-scheduling.md`）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from vs_email_forwarding.config import Settings
from vs_email_forwarding.db.repositories.cleanup import (
    cleanup_expired_messages,
    cleanup_expired_rejected,
)

#: 会话工厂：每次调用开一个短会话，事务边界在本模块（`docs/architecture.md` §5）。
SessionFactory = Callable[[], AsyncSession]


class BatchDeleter(Protocol):
    """「删一批到期行」的契约，由 ``db/repositories/cleanup.py`` 的两个函数满足。"""

    async def __call__(
        self, session: AsyncSession, *, cutoff: datetime, batch_size: int
    ) -> int:  # pragma: no cover - 协议声明，无实现体
        """锁定并删除一批到期行，返回删除的行数。"""
        ...


@dataclass(frozen=True, slots=True)
class CleanupResult:
    """一次清理 tick 删除的行数（按表分列，便于用例与排查核对）。"""

    messages_deleted: int
    rejected_deleted: int


async def cleanup_tick(
    session_factory: SessionFactory,
    settings: Settings,
    *,
    retention: timedelta,
    now: datetime | None = None,
) -> CleanupResult:
    """执行一次保留期清理 tick（`AC-22` 的「执行一次清理动作」就是 ``await`` 它一次）。

    :param session_factory: 每次调用开一个短会话的工厂（`db/session.py` 的会话工厂）。
    :param settings: 配置入口；本 tick 的每批条数取自它的 ``batch_size``。
    :param retention: 保留期时长，由调用方给出（`NFR-8`）。
    :param now: 取时点，缺省用应用侧的 UTC 当前时刻；传入即固定 cutoff，便于用例确定化。
    """
    cutoff = (now if now is not None else datetime.now(UTC)) - retention
    batch_size = settings.batch_size
    return CleanupResult(
        messages_deleted=await _delete_every_due_batch(
            session_factory,
            cutoff=cutoff,
            batch_size=batch_size,
            delete_batch=cleanup_expired_messages,
        ),
        rejected_deleted=await _delete_every_due_batch(
            session_factory,
            cutoff=cutoff,
            batch_size=batch_size,
            delete_batch=cleanup_expired_rejected,
        ),
    )


async def _delete_every_due_batch(
    session_factory: SessionFactory,
    *,
    cutoff: datetime,
    batch_size: int,
    delete_batch: BatchDeleter,
) -> int:
    """反复取批删除，直到某一批一条也删不掉，返回本次 tick 的删除总数。

    每批一个短事务（锁行与删除在同一事务内，`design.md` §3.6）；被并发执行体
    锁走（``SKIP LOCKED``）的行本轮删不到，留到下一轮，不报错、不阻塞。
    """
    deleted_total = 0
    while True:
        async with session_factory() as session:
            deleted = await delete_batch(session, cutoff=cutoff, batch_size=batch_size)
            await session.commit()
        deleted_total += deleted
        if deleted == 0:
            return deleted_total


__all__ = ["BatchDeleter", "CleanupResult", "SessionFactory", "cleanup_tick"]
