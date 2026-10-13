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


def session_factory() -> async_sessionmaker[AsyncSession]:
    """会话工厂：绑定进程级单例引擎，`expire_on_commit=False`；每次调用开一个短会话。

    `async_sessionmaker` 的默认会话类就是 `AsyncSession`，因此不必重复传 `class_`。
    """
    return async_sessionmaker(bind=get_engine(), expire_on_commit=False)
