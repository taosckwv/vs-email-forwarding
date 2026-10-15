#!/usr/bin/env python3
"""健康检查接口的单元测试（`FR-14` / `AC-20`）。

`/healthz` 的数据库探测是一个**可覆盖的 FastAPI 依赖**
（`api/v1/dependencies.py::database_available`，它再经 `service/health.py` 走 `db/`），
因此本文件用 `dependency_overrides` 注入假探测——`tests/unit/` **不依赖数据库与网络**
（`AGENTS.md` §4），这里既不建引擎也不连真库。

期望值全部是手写字面量，来源不是被测代码：

* 探测不可用 → `503`，响应体恰好是 `{"detail": "数据库不可用"}`——不含 DSN / 凭据 / 主机名
  （所有者 2026-10-15 裁决 A；`NFR-5`、`AGENTS.md` §8.4；落点 `design.md` §4.1 与
  `docs/api-contract.yaml` 的 `/healthz` 条目）。
* 探测可用 → `200`，`HealthResponse` 的 `status` 恒为 `ok`、`version` 与包版本同源。
* 鉴权占位：挂在 `api_router` 上、当前为空、替换成真实校验不重排路由结构
  （`design.md` §4.1「一期不做 API 鉴权」、`docs/open-questions.md` F3）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from typing import Final, cast

import pytest
import structlog
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from vs_email_forwarding import __version__
from vs_email_forwarding.api.v1 import api_router, dependencies
from vs_email_forwarding.api.v1.dependencies import database_available, require_auth
from vs_email_forwarding.logging import configure_logging

#: 注入的探测失败原文：借真实的连接串形状当「异常文本」的探针——它一旦出现在日志里就是泄露
#: （`NFR-5`、`AGENTS.md` §8 第 4 条）。
_DATABASE_PROBE_FAILURE_MARKER: Final = "postgresql://user:pw@db-host:5432/x"

#: traceback 的头部文本：用它判定「留栈」与否（`code-style.md` §2.1 例外三要求不留栈）。
_TRACEBACK_HEADER: Final = "Traceback (most recent call last):"


@pytest.fixture()
def probe_calls() -> list[str]:
    """假探测的调用记录：用它证明 `/healthz` 走的是被覆盖的依赖，而不是真探测。"""
    return []


@pytest.fixture()
def app(client: TestClient, probe_calls: list[str]) -> Iterator[FastAPI]:
    """把探测依赖覆盖成「数据库可用」的假实现；用例结束即清空覆盖。"""
    application = cast(FastAPI, client.app)

    async def _probe_ok() -> bool:
        probe_calls.append("probe")
        return True

    application.dependency_overrides[database_available] = _probe_ok
    yield application
    application.dependency_overrides.clear()


def test_healthz_returns_ok(app: FastAPI, client: TestClient, probe_calls: list[str]) -> None:
    """GET /healthz：探测可用 → 200，且用的是被覆盖的假探测（单测不连真库）。"""
    response = client.get("/healthz")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["version"] == __version__
    assert probe_calls == ["probe"]


def test_healthz_returns_503_when_probe_unavailable(app: FastAPI, client: TestClient) -> None:
    """探测不可用 → 503；响应体只有一句「数据库不可用」（`NFR-5`：不带连接信息）。"""

    async def _probe_down() -> bool:
        return False

    app.dependency_overrides[database_available] = _probe_down

    response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json() == {"detail": "数据库不可用"}


def test_database_available_folds_probe_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """探测抛错时依赖返回 `False`（异常文本不外泄，也不向上抛给框架去当 500）。"""

    async def _probe_raises() -> None:
        raise ConnectionError("连接被拒")

    monkeypatch.setattr(dependencies, "check_database_use_case", _probe_raises)
    assert asyncio.run(database_available()) is False

    async def _probe_fine() -> None:
        return None

    monkeypatch.setattr(dependencies, "check_database_use_case", _probe_fine)
    assert asyncio.run(database_available()) is True


@pytest.fixture()
def restore_logging_state() -> Iterator[None]:
    """用例结束还原进程级日志状态（同 `tests/unit/test_logging_output.py` 的还原口径）。"""
    yield
    structlog.reset_defaults()
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
    root_logger.setLevel(logging.WARNING)


def test_probe_failure_is_logged_without_exception_text(
    restore_logging_state: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """探测失败留痕：事件名 + `exc_type`，不留栈、不记异常文本（`code-style.md` §2.1 例外三）。

    断言落在**实际写到流上的日志**（`docs/testing.md` §4.5）：登记条目要求的两条可观察项——
    按 `exc_type` 记事件、不留栈——在这里各由一条判据盯住。驱动的异常原文可能带上连接串里的
    主机名（`NFR-5`、`AGENTS.md` §8 第 4 条），故注入的原文与 traceback 都不得出现在日志里；
    事件必须真的落到应用日志（§2.2「静默吞掉 = 隐患」）。

    日志配置放在**用例体内**：`capsys` 逐阶段换缓冲，夹具里配置会让输出落进 setup 阶段的缓冲，
    读不到（`tests/unit/test_logging_output.py` 同款写法）。logger 代理则是重绑过的——
    `configure_logging` 打开了 `cache_logger_on_first_use`，模块级代理一旦在别处用过就固定沿用
    旧配置，重绑一个「刚刚拿到」的代理才能与执行顺序无关（R12）。
    """
    configure_logging("INFO", "json")
    monkeypatch.setattr(dependencies, "logger", structlog.get_logger(dependencies.__name__))

    async def _probe_raises() -> None:
        raise ConnectionError(_DATABASE_PROBE_FAILURE_MARKER)

    monkeypatch.setattr(dependencies, "check_database_use_case", _probe_raises)

    assert asyncio.run(database_available()) is False

    output = capsys.readouterr().err
    records = [json.loads(line) for line in output.splitlines() if line.strip()]
    assert len(records) == 1, output
    assert records[0]["event"] == "health_database_probe_failed"
    assert records[0]["level"] == "warning"
    assert records[0]["exc_type"] == "ConnectionError"
    assert records[0]["logger"] == "vs_email_forwarding.api.v1.dependencies"
    # 不留栈、不记异常文本：`exception` 字段与异常原文都不该出现。
    assert "exception" not in records[0]
    assert _DATABASE_PROBE_FAILURE_MARKER not in output
    assert _TRACEBACK_HEADER not in output


def test_auth_placeholder_is_router_level_and_empty() -> None:
    """鉴权占位：`api_router` 级依赖、当前为空；替换成真实校验不重排路由结构（F3）。"""
    assert [dependency.dependency for dependency in api_router.dependencies] == [require_auth]

    # 当前为空：调用即通过，不抛异常、不返回任何东西。
    assert asyncio.run(require_auth()) is None


def test_auth_placeholder_replacement_takes_effect(app: FastAPI, client: TestClient) -> None:
    """占位换成真实校验后立刻生效（本用例换成「一律拒绝」）——不需要重排路由（F3）。"""

    async def _deny() -> None:
        raise HTTPException(status_code=401, detail="未授权")

    app.dependency_overrides[require_auth] = _deny

    assert client.get("/healthz").status_code == 401
