#!/usr/bin/env python3
"""五条后台任务的统一调度：各自独立循环、独立间隔、各自吞异常（spec 0001 / T-22）。

设计真源：`specs/0001-email-forwarding-service/design.md` §3（后台任务共五条）、
`plan.md` §2.4 与 §5.1（流的独立性）、`docs/research/02-runtime-and-scheduling.md`
（进程内 asyncio、随 FastAPI lifespan 启停）。

口径：

* **间隔只从配置读**（`NFR-2` / `AGENTS.md` §8.2）：五条任务的间隔一律经
  :class:`~vs_email_forwarding.config.Settings` 的 ``POLL_*_SECONDS`` 字段取得，
  本模块与其余 ``flows/`` 模块的源码里不出现间隔 / 批量字面量——`AC-23` 的静态检查
  扫的正是 ``flows/``。
* **各条任务互不影响**（`plan.md` §5.1）：单条流的一次 tick 抛异常只留一条结构化
  错误日志，循环继续；某条流阻塞也不影响其余四条。
* **不做端到端进程测试**（`plan.md` §7.1）：这里只是「每 N 秒 ``await`` 一次 tick」的
  封装，真实行为由「单次 tick 是普通 async 函数、测试里直接 ``await``」覆盖。
* 统一启停由 :class:`Scheduler` 的 :meth:`~Scheduler.start` /
  :meth:`~Scheduler.stop` 提供，调用点是 ``main.py`` 的 lifespan（装配在 T-32）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from enum import StrEnum
from typing import Final

import structlog

from vs_email_forwarding.config import Settings

logger = structlog.get_logger(__name__)

#: 单次 tick 的形态：零参、可 ``await`` 的可调用对象（`plan.md` §7.1 的可测性结论）。
#: 返回值不被调度器使用，故放宽到 ``Awaitable[object]``，让各条流返回自己的计数对象。
Tick = Callable[[], Awaitable[object]]


class TaskName(StrEnum):
    """五条后台任务的标识（与 `plan.md` §2.4 的 ``flows/`` 模块一一对应）。"""

    REVIEW_OUTBOUND = "review_outbound"
    SEND_EMAIL = "send_email"
    FETCH_INBOUND = "fetch_inbound"
    REVIEW_INBOUND = "review_inbound"
    CLEANUP = "cleanup"


#: 每条任务间隔的配置读取器：只认 :class:`Settings` 上的 ``POLL_*_SECONDS`` 字段。
_INTERVAL_READERS: Final[Mapping[TaskName, Callable[[Settings], int]]] = {
    TaskName.REVIEW_OUTBOUND: lambda settings: settings.poll_review_outbound_seconds,
    TaskName.SEND_EMAIL: lambda settings: settings.poll_send_seconds,
    TaskName.FETCH_INBOUND: lambda settings: settings.poll_fetch_inbound_seconds,
    TaskName.REVIEW_INBOUND: lambda settings: settings.poll_review_inbound_seconds,
    TaskName.CLEANUP: lambda settings: settings.poll_cleanup_seconds,
}


class Scheduler:
    """五条后台任务的启停与调度，由 ``main.py`` 的 lifespan 驱动。"""

    def __init__(self, settings: Settings, ticks: Mapping[TaskName, Tick]) -> None:
        """构造时**一次性**从配置读出五条任务的间隔（`AC-23` 的观测口径）。

        传入的 ``ticks`` 必须与五条任务一一对应：缺一条或名字不认识都当场报错，
        否则「某条流静默没被调度」只会在生产上才发现（`design.md` §3 的「共五条」）。
        """
        missing = sorted(set(_INTERVAL_READERS) - set(ticks))
        unknown = sorted(set(ticks) - set(_INTERVAL_READERS))
        if missing or unknown:
            raise ValueError(f"后台任务集合与五条任务不符：缺 {missing}，多 {unknown}")

        self._ticks: Mapping[TaskName, Tick] = dict(ticks)
        self._intervals: Mapping[TaskName, int] = {
            name: reader(settings) for name, reader in _INTERVAL_READERS.items()
        }
        self._loops: list[asyncio.Task[None]] = []

    def interval_seconds(self, name: TaskName) -> int:
        """返回某条任务**构造时**读到的间隔秒数。"""
        return self._intervals[name]

    async def start(self) -> None:
        """为每条任务各起一个独立循环（重复启动直接报错，避免双份轮询）。"""
        if self._loops:
            raise RuntimeError("调度器已经启动")
        self._loops = [
            asyncio.create_task(self._run_loop(name), name=f"flow:{name}") for name in self._ticks
        ]

    async def stop(self) -> None:
        """取消五条循环并等待它们退出（lifespan 关闭阶段调用，可重复调用）。"""
        loops, self._loops = self._loops, []
        for loop in loops:
            loop.cancel()
        await asyncio.gather(*loops, return_exceptions=True)

    async def _run_loop(self, name: TaskName) -> None:
        """一条任务的循环：跑一次 tick，睡到下一个间隔，如此往复。

        ``CancelledError`` 不被下面的捕获命中（它继承 ``BaseException``），因此
        :meth:`stop` 的取消能正常穿透、让循环优雅退出。
        """
        tick = self._ticks[name]
        interval_seconds = self._intervals[name]
        while True:
            try:
                await tick()
            # 单条流的异常不得传染其余四条（plan.md §5.1「各自吞异常」）：这里的兜底捕获
            # 是 docs/code-style.md §2.1 登记在案的「监督循环」例外——只包住这一次 tick
            # 调用、必须留栈、不吞取消（同 §2.2 的落法）。
            except Exception:
                logger.exception("flow_tick_failed", flow=str(name))
            await asyncio.sleep(interval_seconds)


__all__ = ["Scheduler", "TaskName", "Tick"]
