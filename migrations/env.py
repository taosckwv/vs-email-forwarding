#!/usr/bin/env python3
"""Alembic 迁移环境：用异步引擎跑迁移，连接串只从配置层取。

口径：

* **连接串来源**：唯一入口是 ``config.get_settings().database_url``，本模块**不直接访问
  进程环境变量**（`AGENTS.md` §7：配置只从 ``config`` 读）。该字段
  本身按 pydantic-settings 口径读环境变量 ``DATABASE_URL``（**优先**）与 ``.env``，因此集成测试
  用「环境变量覆盖、不改仓库文件」的口径照旧成立；``alembic.ini`` 里不放连接串。
* **引擎**：``async_engine_from_config`` + ``poolclass=NullPool``——迁移是一次性进程，
  连接用完即弃，不缓存也不复用连接池。
* **``target_metadata``**：``db/models.py`` 的 ``metadata``，是迁移的 DDL 真源
  （`specs/0001-email-forwarding-service/design.md` §2）。
* **不支持 ``--sql`` 离线模式**：本项目只在容器内直连数据库跑迁移（`deploy/migrate.sh`）。
  离线模式会掩盖驱动与连接串问题，因此这里**显式报错**而不是悄悄按在线路径执行。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from vs_email_forwarding.config import get_settings
from vs_email_forwarding.db import models

# Alembic 的 Config 对象：对应正在使用的 alembic.ini。
config = context.config

# 按 alembic.ini 的日志段配置日志。
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 迁移的 DDL 真源：ORM 模型的 metadata（四张表、索引、CHECK 都在这里）。
target_metadata = models.metadata


def database_url() -> str:
    """迁移用的连接串：只从配置层取（环境变量覆盖由 ``get_settings()`` 自己处理）。"""
    return get_settings().database_url


def run_migrations_offline() -> None:
    """拒绝离线（``--sql``）模式：见模块开头的说明。"""
    raise RuntimeError(
        "本项目不支持 Alembic 离线模式（--sql）：迁移一律直连数据库执行，入口是 deploy/migrate.sh"
    )


def do_run_migrations(connection: Connection) -> None:
    """在给定的同步连接上跑迁移（由 ``run_sync`` 从异步连接切换进来）。"""
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """建异步引擎、连一次库、把迁移跑在它的同步封装上，然后释放引擎。"""
    configuration = config.get_section(config.config_ini_section, {})
    # 直接把 URL 交给引擎构造，绕开 ConfigParser 的插值（连接串里可能有 `%`）。
    configuration["sqlalchemy.url"] = database_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """在线模式（唯一支持的模式）。"""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
