#!/usr/bin/env python3
"""数据库可用性探测用例（`/healthz` 用；`FR-14`）。

本模块是 `api/` 与 `db/` 之间的那一层：`api/` 只认 `service/`（`docs/architecture.md` §2、
`design.md` §8），SQL 只在 `db/`（`AGENTS.md` §7）。因此这里开一次短会话——事务边界属于
`service/`（`docs/architecture.md` §5）——在会话上做一次无副作用的探测查询
（`db/session.py::probe_database`），失败原样向上抛，由 `api/v1` 的依赖折叠成「不可用」、
路由转成 `503`（`design.md` §4.1，所有者 2026-10-15 裁决）。

**不重试、不吞异常**：健康检查要回答的正是「此刻能不能用」；吞掉失败等于把「数据库不可用」
说成「一切正常」。
"""

from __future__ import annotations

from vs_email_forwarding.db.session import probe_database, session_factory


async def check_database() -> None:
    """开一次短会话做数据库探测（查询语句在 `db/session.py`）；不可用时抛异常。"""
    factory = session_factory()
    async with factory() as session:
        await probe_database(session)


__all__ = ["check_database"]
