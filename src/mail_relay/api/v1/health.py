"""健康检查路由。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from mail_relay import __version__
from mail_relay.config import get_settings

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """健康检查响应体。"""

    status: Literal["ok"]
    app_env: str
    version: str


@router.get("/healthz", response_model=HealthResponse, summary="健康检查")
async def healthz() -> HealthResponse:
    """返回进程存活状态。

    待补：数据库连通性检查，以及四条轮询流的存活状态（例如最后一次 tick 时间）。
    这两项将在数据库层与调度器落地后补充，预研阶段不实现。
    """
    settings = get_settings()
    return HealthResponse(status="ok", app_env=settings.app_env, version=__version__)


__all__ = ["HealthResponse", "healthz", "router"]
