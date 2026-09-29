"""API v1 路由聚合。

负责把 v1 版本下的各子路由拼装成一个 ``api_router`` 供 ``main.create_app()`` 挂载。
当前只有健康检查路由；投递与回复查询路由的契约字段尚未定案，见
``docs/open-questions.md``。
"""

from __future__ import annotations

from fastapi import APIRouter

from mail_relay.api.v1 import health

api_router = APIRouter()
api_router.include_router(health.router)

__all__ = ["api_router"]
