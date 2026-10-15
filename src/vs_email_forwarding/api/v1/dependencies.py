#!/usr/bin/env python3
"""router 级依赖：鉴权占位（F3）与可覆盖的数据库探测依赖。

* `require_auth` —— **鉴权占位，当前为空**：挂在 `api_router` 上（见 `api/v1/__init__.py`），
  后续接真实校验（API Key / mTLS / 内网白名单，口径见 `docs/open-questions.md` F3）时只换这一个
  依赖的实现，不重排路由结构（`design.md` §4.1）。
* `database_available` —— `/healthz` 的数据库探测：经 `service/health.py` 走 `db/`，本层不 import
  `db/`、不写 SQL（`docs/architecture.md` §2、`design.md` §8）。它只回一位事实（可用 / 不可用），
  **把「不可用」翻成 `503` 的是路由本身**（`design.md` §4.1）——探测失败的异常类型取决于驱动，
  本层不解释异常文本（它可能带上连接串里的主机名，`NFR-5`）；失败按 `exc_type` 记一条事件、
  不留栈，让「数据库不可用」在应用日志里看得见又不泄露连接信息（`docs/code-style.md` §2.1 例外三）。
  做成**依赖**是为了可覆盖：`tests/unit` 用 `app.dependency_overrides[database_available]` 注入
  假探测，单测因此不连真库（`AGENTS.md` §4）。
"""

from __future__ import annotations

import structlog

from vs_email_forwarding.service.health import check_database as check_database_use_case

logger = structlog.get_logger(__name__)


async def require_auth() -> None:
    """鉴权占位：当前为空——调用即通过（一期不做鉴权，`docs/open-questions.md` F3）。"""


async def database_available() -> bool:
    """探测数据库：可用 `True`，不可用 `False`（失败不向上抛，只折叠成一位事实）。"""
    try:
        await check_database_use_case()
    except Exception as exc:  # 宽捕获的口径见 code-style.md §2.1 例外三
        # 捕获面只能是 Exception：探测失败的异常类型取决于驱动，而 api 层不得 import db/ 与驱动
        # 类型（docs/architecture.md §2）。只记类型、不留栈——驱动异常原文可能带上连接串里的
        # 主机名或凭据（NFR-5、AGENTS.md §8 第 4 条）；CancelledError 继承 BaseException，
        # 本就不在该捕获面内。
        logger.warning("health_database_probe_failed", exc_type=type(exc).__name__)
        return False
    return True


__all__ = ["database_available", "require_auth"]
