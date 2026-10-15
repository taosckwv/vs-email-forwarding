#!/usr/bin/env python3
"""数据库层。

职责：
  * 异步引擎与连接池、会话工厂（asyncpg 驱动）；
  * ORM 模型定义；
  * **仓储层**——本仓库所有 SQL 只允许出现在这里，流处理层与路由层不得裸写 SQL。

不放：业务规则。

表结构（四张表、索引、CHECK 与状态机）已在 ``specs/0001-email-forwarding-service/design.md``
§2 定案：ORM 模型见 ``models.py``，引擎与会话见 ``session.py``，仓储层（所有 SQL）见
``repositories/``；Alembic 迁移在仓库根的 ``migrations/``。
"""
