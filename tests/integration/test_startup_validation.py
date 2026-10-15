#!/usr/bin/env python3
"""启动校验的集成面：三条判据各自违规 → 进程非零退出且**不监听 HTTP 端口**（T-38 / `AC-32`）。

真源：`spec.md` 的 `AC-32` 原文（三条判据各自单独制造一次违规、进程拒绝启动、不开始监听）、
`design.md` §4.4（三条判据与「校验信息只出现配置项名」）与 §4.4 的调用点口径（校验在
`lifespan` 的显式启动步骤，不在 import 期）；三条判据的**单测面**（判据语义与调用点）由既有的
`tests/unit/test_settings_validation.py` 覆盖，本文件只判「真的起不来」这件事——因此用
**子进程**起真实应用，而不是在测试进程里调一次 `validate_startup()`：后者证明不了
「不监听端口」。

手法与判据（`plan.md` §7.3 的 `test_startup_validation.py` 一行）：

* 环境变量整体继承本进程（含 T-33 夹具写的隔离库、替身 SMTP 与 `example.com` 占位身份），
  只覆写**被测判据涉及的那一个**——这样「只有一条判据违规」是可判定的；
* 子进程用 `sys.executable -m uvicorn …` 起真实服务（当前解释器就是 `uv run` 的那个 venv）；
* 断言四件：退出码非零、输出里没有 uvicorn 的启动成功行、**没有端口在监听**、
  违规的那条判据被点名（且不回显地址本身，`NFR-5` / `AGENTS.md` §8 第 4 条）；
* 另有一条**正控**：同一套环境不覆写任何判据时服务能起来并 `GET /healthz` 返回 200——
  否则上面三条「起不来」可能只是因为 uvicorn 根本跑不起来（假绿）。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Final

import pytest

pytestmark = pytest.mark.integration

#: 应用入口（与 `scripts/dev-up.sh` / 部署口径同一个目标）。
_APP_TARGET: Final = "vs_email_forwarding.main:app"

#: 违规启动的子进程必须在此时限内自己结束（超时即判失败：卡住不退出同样是「没有拒绝启动」的证据缺失）。
_STARTUP_TIMEOUT_SECONDS: Final = 30.0

#: 正控等待服务的上界（`design.md` §4.4 只要求拒绝/接受启动，不判启动耗时）。
_SERVE_WAIT_SECONDS: Final = 30.0

_PLACEHOLDER_REPLY_TO: Final = "reply-slot@example.com"
_PLACEHOLDER_OTHER: Final = "other-slot@example.com"

#: `AC-32` 的三条判据各造一次违规：`(覆写环境变量, 被点名的配置项名, 不得出现在输出里的值)`。
_VIOLATIONS: Final = (
    # 判据 1：被拉取信箱地址与 REPLY_TO_ADDRESS 不同。
    ({"IMAP_ADDRESS": _PLACEHOLDER_OTHER}, "IMAP_ADDRESS", _PLACEHOLDER_OTHER),
    # 判据 2：MAIL_FROM_ADDRESS 取不到邮件域。
    ({"MAIL_FROM_ADDRESS": ""}, "MAIL_FROM_ADDRESS", ""),
    # 判据 3：REPLY_TO_ADDRESS 本身不是合法地址（无 `@`、含空白）。
    # 与判据 1 隔离：IMAP_ADDRESS 一并覆写成同一个值，两条因此相等，只有判据 3 会失败。
    (
        {"REPLY_TO_ADDRESS": "not an address", "IMAP_ADDRESS": "not an address"},
        "REPLY_TO_ADDRESS",
        "not an address",
    ),
)


def _free_port() -> int:
    """取一个当前空闲的端口（先绑 0 再释放；子进程随后绑它）。

    **已知 TOCTOU 窗口**（`T-38` 评审 S-1 登记，`low`）：本函数释放端口到子进程重新绑定之间，
    端口可能被同机其他进程抢走。按 `_run_to_exit` 的实际路径，后果分三种：
    ① **违规用例假红**：抢占者在听时，违规用例不会走到「进程自行退出」那一步，失败点落在
    违规用例的**首个断言** ``assert not startup.listening``（而不是「输出里缺判据名」）；
    ② **正控假红**：合法配置的正控表现为子进程起不来（绑定失败即退出）、`/healthz` 查询超时，
    或抢占者占住端口后迟迟不退（同样走超时路径）；③ **更严重的一种——假绿**：抢占者恰好应答
    `/healthz`（返回 200 且 ``body.status == "ok"``）时，正控可能在真服务从未启动的情况下通过
    ——这一条是**唯一的假绿路径**，上面两条都是假红。本套件单进程顺序运行、窗口实测**数百毫秒**
    （`T-38` 全套复跑稳定），故按 `low` 登记、不加绑定重试；若将来出现并发跑集成套件的场景，
    就在这里改成「绑定失败时重取端口重试一次」，并重新评估后果 ③。
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _is_listening(port: int) -> bool:
    """该端口上是否有人接受连接。"""
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _server_command(port: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "uvicorn",
        _APP_TARGET,
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "info",
    ]


@dataclass(frozen=True, slots=True)
class _Startup:
    """子进程启动结果：退出码、合并输出、以及「有没有开始监听」「有没有自己退出」。"""

    returncode: int
    output: str
    listening: bool
    exited_on_its_own: bool


def _run_to_exit(overrides: dict[str, str]) -> _Startup:
    """用覆写后的环境起一次服务，边等边看端口；一旦开始监听就立刻收掉进程。

    不等固定长超时：**「端口开始接受连接」本身就是「没有拒绝启动」的证据**，这时收掉进程、
    带着这条证据失败即可（`design.md` §4.4：启动校验要先拒掉配置，服务才能开始监听）。
    """
    port = _free_port()
    proc = subprocess.Popen(
        _server_command(port),
        env={**os.environ, **overrides},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    listening = False
    deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
    while proc.poll() is None and time.monotonic() < deadline:
        if _is_listening(port):
            listening = True
            break
        time.sleep(0.1)
    exited_on_its_own = proc.poll() is not None
    output = _reap(proc)
    return _Startup(
        returncode=proc.returncode if proc.returncode is not None else -1,
        output=output,
        listening=listening or _is_listening(port),
        exited_on_its_own=exited_on_its_own,
    )


def _reap(proc: subprocess.Popen[str]) -> str:
    """收掉子进程并取回合并输出（进程已退出时只是取输出）。"""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=15.0)
        except subprocess.TimeoutExpired:  # pragma: no cover - 收尾兜底
            proc.kill()
            proc.wait()
    return proc.stdout.read() if proc.stdout is not None else ""


@pytest.mark.parametrize(
    ("overrides", "expected_name", "forbidden_value"),
    _VIOLATIONS,
    ids=["imap_address_mismatch", "mail_from_without_domain", "reply_to_invalid"],
)
def test_violation_refuses_to_start_and_does_not_listen(
    overrides: dict[str, str], expected_name: str, forbidden_value: str
) -> None:
    """违规配置：自行非零退出、从未监听端口、无启动成功行、点名判据且不回显地址（`AC-32`）。"""
    startup = _run_to_exit(overrides)

    assert not startup.listening, "违规配置下服务开始监听端口了"
    assert startup.exited_on_its_own, "违规配置下进程没有自行退出（被用例收掉了）"
    assert startup.returncode != 0, f"违规配置下进程仍以 0 退出：\n{startup.output}"
    assert "Application startup complete" not in startup.output, "违规配置下服务仍完成了启动"
    assert expected_name in startup.output, (
        f"退出信息没有点名判据 {expected_name}：\n{startup.output}"
    )
    if forbidden_value:
        assert forbidden_value not in startup.output, "退出信息回显了地址本身（NFR-5 的日志红线）"


def test_valid_configuration_starts_and_serves_healthz() -> None:
    """正控：不覆写任何判据时服务能起来并 `GET /healthz` 返回 200（否则上面的「起不来」不可信）。"""
    port = _free_port()
    proc = subprocess.Popen(
        _server_command(port),
        env={**os.environ},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        body = _wait_for_healthz(port, proc)
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=15.0)
        except subprocess.TimeoutExpired:  # pragma: no cover - 收尾兜底
            proc.kill()
            proc.communicate()
    assert body["status"] == "ok"


def _wait_for_healthz(port: int, proc: subprocess.Popen[str]) -> dict[str, object]:
    """轮询 `/healthz` 直到 200 或超时；超时即带上子进程输出失败。"""
    deadline = time.monotonic() + _SERVE_WAIT_SECONDS
    url = f"http://127.0.0.1:{port}/healthz"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            output = proc.stdout.read() if proc.stdout is not None else ""
            raise AssertionError(f"服务提前退出（code={proc.returncode}）：\n{output}")
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                assert response.status == 200
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError:
            time.sleep(0.2)
    raise AssertionError(f"{_SERVE_WAIT_SECONDS}s 内 /healthz 未返回 200（端口 {port}）")
