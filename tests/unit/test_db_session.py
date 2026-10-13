"""`db/session.py` 的生命周期形状：懒建的进程级单例引擎、按阶段开的短会话工厂。

**不连数据库**（`docs/testing.md` §3.1）：只检查引擎与会话工厂的构造与身份，不建立任何连接。
连接池在真实 PostgreSQL 上的行为（并发、事务边界、`dispose()` 之后连接不可用）留
`tests/integration/`。

期望值来源（`docs/testing.md` R2）：

* 引擎是**进程级单例**、会话**按阶段开随阶段关**、lifespan 退出统一 `dispose()`：
  `docs/architecture.md` §5；
* `expire_on_commit=False`（`service/` 在会话关闭后仍要读 ORM 行并转 Pydantic）：
  `design.md` §8；
* 连接串是异步驱动 `postgresql+asyncpg`、且只来自 `config.get_settings().database_url`：
  `design.md` §5 的依赖选型、`AGENTS.md` §7「配置只从 config 读」。

单例是进程级全局状态，因此每个用例**前后**都 `dispose_engine()`，用例之间不共享引擎
（R12：可乱序、可重复、不依赖执行顺序）。而「懒建」这条契约从公开接口观测不到，本文件因此读
`session._engine` 这个私有槽位：用 `importlib.reload` 重放一次模块体，等价于「全新 import」，
此时槽位必须仍是空的——这是唯一能观测「还没有引擎」的位置。
"""

from __future__ import annotations

import ast
import importlib
import pathlib
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from vs_email_forwarding.db import session

SESSION_SOURCE = pathlib.Path(session.__file__)


@pytest.fixture(autouse=True)
async def _isolated_engine() -> AsyncIterator[None]:
    """用例之间不共享进程级单例（`docs/architecture.md` §5 的单例只在进程内共享）。"""
    await session.dispose_engine()
    yield
    await session.dispose_engine()


def test_importing_the_module_does_not_create_an_engine() -> None:
    # 重新执行模块体（等价于一次全新 import）：import 期不得创建引擎，也不得有任何调用。
    refreshed = importlib.reload(session)
    assert refreshed._engine is None


def test_get_engine_is_a_process_level_singleton() -> None:
    first = session.get_engine()
    second = session.get_engine()

    assert isinstance(first, AsyncEngine)
    assert first is second


def test_create_engine_from_settings_builds_a_fresh_engine() -> None:
    # 该函数是「建引擎」本身，不进单例；单例语义只属于 get_engine()。
    first = session.create_engine_from_settings()
    second = session.create_engine_from_settings()

    assert first is not second
    assert first is not session.get_engine()


async def test_dispose_engine_resets_the_singleton() -> None:
    before = session.get_engine()

    await session.dispose_engine()

    assert session.get_engine() is not before


async def test_dispose_engine_disposes_the_pool_of_the_released_engine() -> None:
    engine = session.get_engine()
    pool_before = engine.pool

    await session.dispose_engine()

    # dispose 会换掉连接池：这是「连接确实被释放」的可观测后果，而不是「某方法被调用过」。
    assert engine.pool is not pool_before


async def test_session_factory_produces_sessions_bound_to_the_singleton_engine() -> None:
    factory = session.session_factory()

    assert isinstance(factory, async_sessionmaker)
    async with factory() as created:
        assert isinstance(created, AsyncSession)
        assert created.bind is session.get_engine()


async def test_session_factory_disables_expire_on_commit() -> None:
    # design.md §8：会话关闭后 service/ 仍要读 ORM 行，默认的「提交即过期」会在这里抛错。
    # `AsyncSession` 自身不暴露该开关，它落在会话代理的同步会话上（`sync_session`）。
    factory = session.session_factory()
    assert factory.kw["expire_on_commit"] is False

    async with factory() as created:
        assert created.sync_session.expire_on_commit is False


def test_engine_url_comes_from_settings_with_asyncpg_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 连接串只从唯一配置入口取：换一个辨识度高的值，引擎必须跟着走（写死 DSN 会在这里红）。
    url = "postgresql+asyncpg://someone:secret@db.invalid:5432/somewhere_else"
    monkeypatch.setattr(session, "get_settings", lambda: SimpleNamespace(database_url=url))

    for engine in (session.create_engine_from_settings(), session.get_engine()):
        assert engine.url.render_as_string(hide_password=False) == url
        # 驱动是异步的 asyncpg（specs/0001 的依赖选型）。
        assert engine.url.drivername == "postgresql+asyncpg"


def test_module_reads_no_environment_variables_directly() -> None:
    # AGENTS.md §7：配置只从 config 层读，本模块不得再读一次环境变量。
    tree = ast.parse(SESSION_SOURCE.read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported |= {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    referenced = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    referenced |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}

    assert "os" not in imported
    assert not referenced & {"environ", "getenv"}
