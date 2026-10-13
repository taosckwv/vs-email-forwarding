#!/usr/bin/env python3
"""仓储层（`docs/architecture.md`：所有 SQL 只在这层）。

各模块只做语句构造与执行，不承载业务规则；一律 SQLAlchemy 表达式构造器，
禁止 ``text()`` 裸串（`AGENTS.md` §8.1、`specs/0001-email-forwarding-service/plan.md` §2.9）。
"""
