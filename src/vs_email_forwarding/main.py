#!/usr/bin/env python3
"""应用入口：FastAPI 应用工厂与进程生命周期钩子。

可用 ``uvicorn vs_email_forwarding.main:app`` 直接启动。

lifespan 的三步（``design.md`` §4.4 与 ``plan.md`` §2.7）：**显式启动校验**（不在 import 期）
→ 建进程级引擎 → 起五条后台任务；关闭阶段先停任务、再释放引擎。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from functools import partial

import structlog
from fastapi import FastAPI

from vs_email_forwarding import __version__, constants
from vs_email_forwarding.api.v1 import api_router
from vs_email_forwarding.config import get_settings, validate_startup
from vs_email_forwarding.db.session import dispose_engine, get_engine, session_factory
from vs_email_forwarding.flows import (
    cleanup,
    fetch_inbound,
    review_inbound,
    review_outbound,
    send_email,
)
from vs_email_forwarding.flows.scheduler import Scheduler, TaskName, Tick
from vs_email_forwarding.logging import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """进程生命周期钩子：启动校验 → 引擎 → 五条后台任务；关闭时反向收尾。

    1. :func:`~vs_email_forwarding.config.validate_startup` 校验四条判据（``design.md`` §4.4）：
       这是**显式启动步骤**，不放在 import 期——不满足即抛出，进程非零退出、不监听端口。
       ``TLS_VERIFY=false``（显式关闭对外 TLS 校验）在通过校验后记一条结构化告警
       ``tls_verify_disabled``：关闭校验是允许的逃生门，但必须可观察。该事件只带
       ``tls_verify`` 字段，不含地址、口令与路径（`docs/research/04-security-and-review.md`）。
    2. 建进程级引擎（:func:`~vs_email_forwarding.db.session.get_engine` 懒建单例），并取一个
       会话工厂给五条流共用（短会话按阶段开、用完即关）。
    3. 五条任务（四条业务流 + 保留期清理）交给 :class:`Scheduler` 并发启动：间隔只从配置读，
       清理用的保留期取自 ``constants.RETENTION_DAYS``——产品语义常量，既不是字面量也不是配置项。

    关闭阶段先 ``await scheduler.stop()``（取消五条循环并等它们退出），再
    ``await dispose_engine()`` 释放连接池：任务先停，才不会在引擎释放后还去开新会话。
    """
    settings = get_settings()
    validate_startup(settings)
    if not settings.tls_verify:
        # 关闭校验是显式逃生门：连接仍然加密，但「校验被关掉」必须可观察。
        structlog.get_logger(__name__).warning("tls_verify_disabled", tls_verify=False)

    get_engine()
    factory = session_factory()
    ticks: dict[TaskName, Tick] = {
        TaskName.REVIEW_OUTBOUND: partial(review_outbound.review_outbound_tick, factory, settings),
        TaskName.SEND_EMAIL: partial(send_email.send_email_tick, factory, settings),
        TaskName.FETCH_INBOUND: partial(fetch_inbound.fetch_inbound_tick, factory, settings),
        TaskName.REVIEW_INBOUND: partial(review_inbound.review_inbound_tick, factory, settings),
        TaskName.CLEANUP: partial(
            cleanup.cleanup_tick,
            factory,
            settings,
            retention=timedelta(days=constants.RETENTION_DAYS),
        ),
    }
    scheduler = Scheduler(settings, ticks)
    await scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()
        await dispose_engine()


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用实例。"""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    application = FastAPI(
        title="vs-email-forwarding",
        description="邮件中转服务：接收待发邮件、安全审查、SMTP 投递并回收审查回复。",
        version=__version__,
        lifespan=lifespan,
    )
    application.include_router(api_router)
    return application


app = create_app()


__all__ = ["app", "create_app", "lifespan"]
