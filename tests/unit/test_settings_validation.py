#!/usr/bin/env python3
"""启动即校验：三条判据与校验的调用点（spec 0001 / T-32 / `AC-32`）。

期望值出处：`specs/0001-email-forwarding-service/design.md` §4.4——三条判据（`IMAP_ADDRESS`
等于 `REPLY_TO_ADDRESS`、`MAIL_FROM_ADDRESS` 能解析出邮件域、`REPLY_TO_ADDRESS` 合法）、
「任一不满足就**拒绝启动**」（fail-fast），以及「校验信息里只出现**配置项名**，不出现地址
本身」（`NFR-5`、`AGENTS.md` §8 第 4 条）。违规值与合法值都写死在下面，不取自被测实现。

同属 §4.4 的还有校验的**调用点**：`main.py` 的 lifespan 显式启动步骤，**不放在 import 期**。
本文件因此再驱动一次 lifespan（协作者全换成假件，不建引擎、不连库），核对「校验 → 建引擎 →
起五条任务」的次序、五条任务的齐全性、清理任务的保留期绑定，以及关闭阶段的停任务与释放引擎
（`plan.md` §2.7）。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from types import ModuleType
from typing import Final

import pytest
from fastapi import FastAPI

from vs_email_forwarding import constants
from vs_email_forwarding.config import Settings, validate_startup
from vs_email_forwarding.flows.scheduler import TaskName, Tick

#: 合法三元组：三条判据都成立（真源是 `design.md` §4.4 的三条判据本身）。
_MAIL_FROM: Final = "noreply@example.com"
_REPLY_TO: Final = "reply@example.com"

#: 判据 1 的违规值：空串（`IMAP_ADDRESS` 为空即拒启动）与不相等的地址。
_BAD_IMAP_ADDRESSES: Final = ("", "other@example.com")

#: 判据 2 的违规值：取不出邮件域（取域规则见 `design.md` §3.1；`<uuid@>` 是设计点名的残缺值）。
_DOMAINLESS_MAIL_FROM: Final = ("", "noreply", "noreply@", "<uuid@>")

#: 判据 3 的违规值：不含 `@` 或含空白。
_INVALID_REPLY_TO: Final = ("", "reply.example.com", "reply @example.com", "reply@exam ple.com")

#: 「信息不得回显地址」用的三个可识别探针值：各自只触发一条判据。
_LEAKY_IMAP_ADDRESS: Final = "leaky-imap@example.org"
_LEAKY_MAIL_FROM: Final = "leaky-from"
_LEAKY_REPLY_TO: Final = "leaky reply@example.org"


def _valid_settings(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> Settings:
    """只认用例显式给值的配置对象（关掉 `.env`、剔掉同名环境变量，`docs/testing.md` R12）。"""
    for field in ("MAIL_FROM_ADDRESS", "REPLY_TO_ADDRESS", "IMAP_ADDRESS"):
        monkeypatch.delenv(field, raising=False)
    values = {
        "mail_from_address": _MAIL_FROM,
        "reply_to_address": _REPLY_TO,
        "imap_address": _REPLY_TO,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize("bad", _BAD_IMAP_ADDRESSES)
def test_imap_address_must_equal_reply_to(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    """判据 1：`IMAP_ADDRESS` 与 `REPLY_TO_ADDRESS` 相等（为空也拒绝启动）。"""
    with pytest.raises(ValueError) as excinfo:
        validate_startup(_valid_settings(monkeypatch, imap_address=bad))

    message = str(excinfo.value)
    assert "IMAP_ADDRESS" in message
    assert "REPLY_TO_ADDRESS" in message
    if bad:
        assert bad not in message


@pytest.mark.parametrize("bad", _DOMAINLESS_MAIL_FROM)
def test_mail_from_address_must_yield_a_domain(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    """判据 2：`MAIL_FROM_ADDRESS` 能解析出邮件域（`Message-ID` 的域取自它，§3.1）。"""
    with pytest.raises(ValueError) as excinfo:
        validate_startup(_valid_settings(monkeypatch, mail_from_address=bad))

    message = str(excinfo.value)
    assert "MAIL_FROM_ADDRESS" in message
    if bad:
        assert bad not in message


@pytest.mark.parametrize("bad", _INVALID_REPLY_TO)
def test_reply_to_address_must_be_valid(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    """判据 3：`REPLY_TO_ADDRESS` 本身合法（含 `@`、无空白）。

    让 `IMAP_ADDRESS` 与它取同一个值，判据 1 因此不成立、这一条能单独判到判据 3。
    """
    with pytest.raises(ValueError) as excinfo:
        validate_startup(_valid_settings(monkeypatch, reply_to_address=bad, imap_address=bad))

    message = str(excinfo.value)
    assert "REPLY_TO_ADDRESS" in message
    if bad:
        assert bad not in message


def test_valid_configuration_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """三条判据同时成立时校验通过（不抛异常）——否则「拒绝启动」就没有边界。"""
    validate_startup(_valid_settings(monkeypatch))


def test_failure_messages_never_echo_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    """三条判据的失败信息都只出现配置项名，不出现任何地址（`NFR-5`、`AGENTS.md` §8.4）。"""
    cases = (
        ({"imap_address": _LEAKY_IMAP_ADDRESS}, "IMAP_ADDRESS"),
        ({"mail_from_address": _LEAKY_MAIL_FROM}, "MAIL_FROM_ADDRESS"),
        (
            {"reply_to_address": _LEAKY_REPLY_TO, "imap_address": _LEAKY_REPLY_TO},
            "REPLY_TO_ADDRESS",
        ),
    )
    for overrides, expected_name in cases:
        with pytest.raises(ValueError) as excinfo:
            validate_startup(_valid_settings(monkeypatch, **overrides))
        message = str(excinfo.value)
        assert expected_name in message
        for secret in (_MAIL_FROM, _REPLY_TO, *overrides.values()):
            assert secret not in message, f"{secret} 出现在校验信息里（NFR-5）"


def test_retention_days_is_the_product_constant() -> None:
    """保留期是 `NFR-8` 约定的 30 天**产品语义**，落 `constants.py`、不设配置项。"""
    assert constants.RETENTION_DAYS == 30
    assert "RETENTION_DAYS" in constants.__all__


class _Steps:
    """记录 lifespan 各步的调用次序与收到的参数（不建引擎、不起真任务）。"""

    def __init__(self) -> None:
        self.sequence: list[str] = []
        self.validated: Settings | None = None
        self.settings_seen: Settings | None = None
        self.ticks: dict[TaskName, Tick] = {}


class _Harness:
    """`main` 模块与记录器的组合：`main` 在夹具里导入，避免 collection 期触发日志配置。"""

    def __init__(self, module: ModuleType, steps: _Steps) -> None:
        self.module = module
        self.steps = steps


@pytest.fixture()
def lifespan_harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    """把 lifespan 的协作者全部换成假件（`main` 的模块级名字逐个替换，看调用点是否真走）。"""
    from vs_email_forwarding import main

    steps = _Steps()
    settings = _valid_settings(monkeypatch)

    def _fake_validate(received: Settings) -> None:
        steps.sequence.append("validate")
        steps.validated = received

    def _fake_engine() -> object:
        steps.sequence.append("engine")
        return object()

    async def _fake_dispose() -> None:
        steps.sequence.append("dispose")

    class _FakeScheduler:
        def __init__(self, received: Settings, ticks: dict[TaskName, Tick]) -> None:
            steps.settings_seen = received
            steps.ticks = dict(ticks)

        async def start(self) -> None:
            steps.sequence.append("start")

        async def stop(self) -> None:
            steps.sequence.append("stop")

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "validate_startup", _fake_validate)
    monkeypatch.setattr(main, "get_engine", _fake_engine)
    monkeypatch.setattr(main, "session_factory", lambda: "session-factory")
    monkeypatch.setattr(main, "dispose_engine", _fake_dispose)
    monkeypatch.setattr(main, "Scheduler", _FakeScheduler)
    yield _Harness(main, steps)


async def test_lifespan_validates_then_creates_engine_then_starts_tasks(
    lifespan_harness: _Harness,
) -> None:
    """启动次序：显式校验 → 建引擎 → 起任务；校验到的是**实际使用的**那份配置（§4.4）。"""
    harness = lifespan_harness
    async with harness.module.lifespan(FastAPI()):
        assert harness.steps.sequence == ["validate", "engine", "start"]

    assert harness.steps.validated is harness.steps.settings_seen
    assert set(harness.steps.ticks) == set(TaskName)
    assert len(harness.steps.ticks) == 5


async def test_lifespan_shutdown_stops_tasks_and_disposes_engine(
    lifespan_harness: _Harness,
) -> None:
    """关闭阶段：先停五条任务、再释放引擎（`dispose_engine`），次序不留缺口。"""
    harness = lifespan_harness
    async with harness.module.lifespan(FastAPI()):
        pass

    assert harness.steps.sequence == ["validate", "engine", "start", "stop", "dispose"]


async def test_cleanup_task_retention_comes_from_constants(
    lifespan_harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """清理 tick 的保留期来自 `constants.RETENTION_DAYS`（不是写死的字面量 30）。

    改常量后绑定值随之改变——期望值 `timedelta(days=7)` 由用例自己给出，与被测代码无关。
    """
    harness = lifespan_harness
    monkeypatch.setattr(constants, "RETENTION_DAYS", 7)

    async with harness.module.lifespan(FastAPI()):
        pass

    cleanup_tick = harness.steps.ticks[TaskName.CLEANUP]
    assert cleanup_tick.keywords["retention"] == timedelta(days=7)
