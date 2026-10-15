#!/usr/bin/env python3
"""健康检查路由（`FR-14` / `AC-20`）。

路径、状态码与响应体形状的唯一真源是 `docs/api-contract.yaml`；本模块只做 `docs/code-style.md`
§4 的三件事：请求（无入参）→ 读依赖 → 组装响应，其中唯一的错误分支是「探测不可用 → `503`」
（`docs/code-style.md` §4.3）。

**语义是「进程可用」**：HTTP 可达 + 一次轻量数据库探测，**不表达五条后台流是否在跑**——流是否
推进靠日志与独立探针（`design.md` §4.1）。探测实现为一个可覆盖的 FastAPI 依赖
（`api/v1/dependencies.py::database_available`，它经 `service/health.py` 走 `db/`，本层不 import
`db/`），只回一位「可用 / 不可用」的事实；失败时本层转 `503`、响应体只有一句「数据库不可用」，
不含 DSN / 凭据 / 主机名（`NFR-5`）。做成依赖而非模块级单例，是为了 `tests/unit` 能用
`dependency_overrides` 覆盖掉真探测——单测不连数据库（`AGENTS.md` §4）。
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from vs_email_forwarding import __version__
from vs_email_forwarding.api.v1.dependencies import database_available
from vs_email_forwarding.config import get_settings

router = APIRouter(tags=["health"])

#: 与 `docs/api-contract.yaml` 的 `/healthz` 的 `503` 条目逐字一致（契约是唯一真源）。
_NOT_AVAILABLE_DESCRIPTION = "数据库探测失败，进程可用性未达成。"

#: `503` 响应体的 `detail`：只说「数据库不可用」，不带 DSN / 凭据 / 主机名（`NFR-5`）。
_NOT_AVAILABLE_DETAIL = "数据库不可用"


class HealthResponse(BaseModel):
    """健康检查响应体。"""

    status: Literal["ok"]
    app_env: str
    version: str


@router.get(
    "/healthz",
    response_model=HealthResponse,
    summary="健康检查",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"description": _NOT_AVAILABLE_DESCRIPTION}},
)
async def healthz(available: Annotated[bool, Depends(database_available)]) -> HealthResponse:
    """返回进程存活状态；依赖里的数据库探测不可用时抛 `503`。"""
    if not available:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_NOT_AVAILABLE_DETAIL,
        )
    settings = get_settings()
    return HealthResponse(status="ok", app_env=settings.app_env, version=__version__)


__all__ = ["HealthResponse", "healthz", "router"]
