#!/usr/bin/env python3
"""API v1 路由聚合。

负责把 v1 版本下的各子路由拼装成一个 ``api_router`` 供 ``main.create_app()`` 挂载。
当前有两组：消息端点（``messages``：提交与两个查询端点）与健康检查（``health``）。
请求 / 响应契约字段（类型、必填、取值约束）的唯一真源是 ``docs/api-contract.yaml``
（OpenAPI 3.1，``design.md`` §4.1）。
鉴权占位依赖（``dependencies.require_auth``，**当前为空**）挂在 ``api_router`` 上：接真实校验时
只换那一个依赖的实现，不重排路由结构（``design.md`` §4.1，``docs/open-questions.md`` F3）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from vs_email_forwarding.api.v1 import health, messages
from vs_email_forwarding.api.v1.dependencies import require_auth

api_router = APIRouter(dependencies=[Depends(require_auth)])
api_router.include_router(messages.router)
api_router.include_router(health.router)

__all__ = ["api_router"]
