#!/usr/bin/env python3
"""异步引擎与会话工厂：进程级单例引擎 + 按阶段开的短会话。

生命周期口径（`docs/architecture.md` §5）：

* **引擎是进程级单例**：在 FastAPI 的 lifespan 里创建、进程退出时统一 `dispose()`。本模块
  **import 期不建任何引擎**——创建点是 lifespan，首次调用 :func:`get_engine` 才懒建。
* **会话按阶段开、随阶段关**：`api/`、`service/`、`flows/` 都不持常驻会话；一次 tick 的三阶段
  （认领 → 调用 → 落库）各开一个短会话，事务不跨网络调用。
* **连接串只从** `config.get_settings().database_url` **取**：本模块不读环境变量
  （`AGENTS.md` §7 的「配置只从 config 读」）。
* **`expire_on_commit=False`**：`service/` 在会话关闭后仍要读 ORM 行并转 Pydantic（`design.md`
  §8），默认的「提交即过期」会让这一步抛 `DetachedInstanceError`。
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from vs_email_forwarding.config import get_settings

# 进程级单例槽位：``None`` 表示尚未创建（懒建；引擎的创建点是 lifespan，不是 import 期）。
_engine: AsyncEngine | None = None


def create_engine_from_settings() -> AsyncEngine:
    """按配置里的连接串新建一个异步引擎。

    这是「建引擎」本身：**不进单例**，每次调用都返回新实例（池参数用 SQLAlchemy 的默认口径）。
    需要进程内共享的那个引擎请用 :func:`get_engine`。
    """
    return create_async_engine(get_settings().database_url)


def get_engine() -> AsyncEngine:
    """取进程级单例引擎；**首次调用时才创建**（懒建）。"""
    global _engine
    if _engine is None:
        _engine = create_engine_from_settings()
    return _engine


async def dispose_engine() -> None:
    """释放单例引擎并重置单例——下一次 :func:`get_engine` 会拿到新实例。

    lifespan 退出时调用；单测也用它，好让用例之间不共享进程级状态。尚未创建时是空操作。
    """
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


def session_factory(*, isolation_level: str | None = None) -> async_sessionmaker[AsyncSession]:
    """会话工厂：绑定进程级单例引擎，`expire_on_commit=False`；每次调用开一个短会话。

    `async_sessionmaker` 的默认会话类就是 `AsyncSession`，因此不必重复传 `class_`。

    `isolation_level` 是**按次覆盖**的事务隔离级别，`None` 表示用引擎（数据库）默认值。
    唯一的预期调用方是 `T-29` 的查询用例：六态判定要在**同一快照**内做三次仓储读
    （`design.md` §4.1「三次查询同一快照」；PostgreSQL 在 READ COMMITTED 下每条语句各取
    一次快照，所以要真落在同一快照上，只读会话必须提升为 `REPEATABLE READ`）——这与本模块
    里「`expire_on_commit=False`：`service/` 在会话关闭后仍要读 ORM 行」是同一条路径。

    **不得用于写路径**：`REPEATABLE READ` 下的写事务在并发时可能以序列化失败告终，而现有
    写路径（`flows/` 的认领与条件 UPDATE、`service/submit_message.py`）没有重试逻辑，
    它们一律不传本参数。
    """
    engine = get_engine()
    if isolation_level is not None:
        # 隔离级别是**连接级**执行选项（`AsyncSession` / `Session` 自身不接受
        # `isolation_level` 关键字）：绑一个与单例引擎共享同一连接池的分支引擎来按次覆盖。
        # `None` 时不碰 bind，调用方拿到的会话与改动前完全一致。
        engine = engine.execution_options(isolation_level=isolation_level)
    return async_sessionmaker(bind=engine, expire_on_commit=False)


async def probe_database(session: AsyncSession) -> None:
    """在给定会话上做一次 `SELECT 1`——`/healthz` 用的轻量数据库探测。

    所有 SQL 都落在 `db/`（`AGENTS.md` §7）：「探测」这件事本身只是一条无副作用的查询，
    因此它属于本层；谁是调用方（`service/health.py`）、失败怎么表达（`api/` 转 `503`）都不在
    这里决定。会话由调用方传入、事务边界归调用方——同本模块其余函数的既有口径
    （`docs/architecture.md` §5）。

    失败（连不上、认证失败、超时等）**原样向上抛**，不吞异常也不重试：健康检查要回答的正是
    「此刻能不能用」，异常类型取决于驱动，调用方不需要解释它（`design.md` §4.1）。
    """
    await session.execute(text("SELECT 1"))
