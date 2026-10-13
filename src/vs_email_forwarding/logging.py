#!/usr/bin/env python3
"""日志配置层。

统一使用 structlog，并接管标准库 logging（uvicorn、第三方库的日志同样按本模块
配置的格式输出）。禁止使用 ``print``。
"""

from __future__ import annotations

import logging
import sys
from typing import Final

import structlog
from structlog.typing import Processor

# 允许的日志级别名到标准库级别的映射，未命中时回落到 INFO。
_LEVELS: Final[dict[str, int]] = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}

# uvicorn 自带的 logger 需要清空 handler 并交给根 logger，避免日志重复输出。
_UVICORN_LOGGERS: Final[tuple[str, ...]] = ("uvicorn", "uvicorn.error", "uvicorn.access")


def _resolve_level(level: str) -> int:
    """把配置中的级别名解析为标准库级别；无法识别时回落 INFO。"""
    return _LEVELS.get(level.strip().upper(), logging.INFO)


def configure_logging(level: str, fmt: str) -> None:
    """配置 structlog 与标准库 logging。

    :param level: 日志级别名，例如 ``INFO``。
    :param fmt: ``json`` 输出 JSON（生产），其他值输出带颜色的控制台格式（开发）。
    """
    numeric_level = _resolve_level(level)

    # 两条链路共用的 processor：时间戳统一为 UTC ISO8601。
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.dev.set_exc_info,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    if fmt == "json":
        final_processors: list[Processor] = [
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ]
    else:
        final_processors = [
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            # 两种格式口径一致：异常都由 format_exc_info 渲染进 `exception` 字段，
            # 渲染器只负责排版，不再各自渲染一遍（design.md §4.6）。
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(colors=True),
        ]

    # structlog 自身的调用链：统一交给 ProcessorFormatter 渲染。
    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=final_processors,
        # 异常已由各分支的 format_exc_info 渲染进结构化字段；保留在 LogRecord 上会让
        # 标准库 Formatter 再追加一份纯文本 traceback——json 模式因此不再是 JSON-lines
        # （design.md §4.6）。
        keep_exc_info=False,
    )
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    for existing in list(root_logger.handlers):
        root_logger.removeHandler(existing)
    root_logger.addHandler(handler)
    root_logger.setLevel(numeric_level)

    for name in _UVICORN_LOGGERS:
        third_party_logger = logging.getLogger(name)
        third_party_logger.handlers = []
        third_party_logger.propagate = True


__all__ = ["configure_logging"]
