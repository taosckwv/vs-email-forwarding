"""应用入口：FastAPI 应用工厂与进程生命周期钩子。

可用 ``uvicorn mail_relay.main:app`` 直接启动。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from mail_relay import __version__
from mail_relay.api.v1 import api_router
from mail_relay.config import get_settings
from mail_relay.logging import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """进程生命周期钩子：预留四条轮询流的启停位置。

    启动阶段（预研阶段暂不执行任何动作）：
      1. 初始化数据库异步引擎与会话工厂（见 ``mail_relay.db`` 职责说明）；
      2. 并发启动四条轮询流：``review_outbound`` / ``send_email`` /
         ``fetch_inbound`` / ``review_inbound``，间隔取自配置（默认 10 秒）。
         四条流将在此启动与停止，实现见 specs 中的调度规格。

    关闭阶段：取消四条流的 asyncio 任务并等待其优雅退出，随后释放数据库连接池。
    """
    try:
        yield
    finally:
        # 现阶段没有任何后台任务或连接池需要释放。
        pass


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用实例。"""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    application = FastAPI(
        title="mail-relay",
        description="邮件中转服务：接收待发邮件、安全审查、SMTP 投递并回收审查回复。",
        version=__version__,
        lifespan=lifespan,
    )
    application.include_router(api_router)
    return application


app = create_app()


__all__ = ["app", "create_app", "lifespan"]
