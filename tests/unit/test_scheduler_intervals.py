#!/usr/bin/env python3
"""调度器间隔的确定性判定（`AC-23` / `NFR-2`）。

验证的业务规则 / 不变量（真源：`spec.md` 的 `NFR-2` 与 `AC-23`、
`specs/0001-email-forwarding-service/design.md` §3「后台任务共五条」与 §7 的配置项清单、
`plan.md` §7.4 的 `AC-23` 手法）：

* **间隔取自配置**：调度器构造时读到的五条任务间隔 == 各自配置项的取值；改配置后
  重新构造即为新值——这正是 `AC-23` 的「把投递间隔配置改为非 10 秒的值后，服务
  读取并使用的投递间隔随该配置改变」；
* **五条任务一条不少**：任务集合不完整时拒绝构造，不允许「某条流静默没被调度」；
* **源码零字面量**：`flows/` 的源码里不出现间隔与批量字面量——否则
  `POLL_*_SECONDS` / `BATCH_SIZE` 形同虚设（`AGENTS.md` §8.2 的硬约束）；
* **不测墙钟**：本文件不做端到端轮询测试（`plan.md` §7.1 / §7.4 的可测性结论——
  「每 N 秒 await 一次 tick」的封装不为它另写 sleep 用例）。

期望值来源：用例自己写进配置对象的取值（即配置项自身取值），以及 `NFR-2` 与
`design.md` §7 写明的默认值——**不来自被测代码**。
"""

from __future__ import annotations

import ast
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final

import pytest

from vs_email_forwarding.config import Settings
from vs_email_forwarding.flows.scheduler import Scheduler

#: 五条后台任务的名字（真源：`plan.md` §2.4 的 `flows/` 模块清单去掉包与调度器）。
_EXPECTED_TASK_NAMES: Final = (
    "review_outbound",
    "send_email",
    "fetch_inbound",
    "review_inbound",
    "cleanup",
)

#: 任务名 → 该任务间隔所在的配置字段（真源：`design.md` §7 的配置项清单逐项对应）。
_TASK_INTERVAL_FIELDS: Final[dict[str, str]] = {
    "review_outbound": "poll_review_outbound_seconds",
    "send_email": "poll_send_seconds",
    "fetch_inbound": "poll_fetch_inbound_seconds",
    "review_inbound": "poll_review_inbound_seconds",
    "cleanup": "poll_cleanup_seconds",
}

#: 规格写明的默认值（真源：`NFR-2` 的「默认 10 秒」、`design.md` §7 的
#: `POLL_CLEANUP_SECONDS` 默认 3600 与 `BATCH_SIZE` 默认 100）。
_DOCUMENTED_DEFAULTS: Final[dict[str, int]] = {
    "poll_review_outbound_seconds": 10,
    "poll_send_seconds": 10,
    "poll_fetch_inbound_seconds": 10,
    "poll_review_inbound_seconds": 10,
    "poll_cleanup_seconds": 3600,
    "batch_size": 100,
}

#: 必须只经配置读取的字段：它们以字面量出现在 `flows/` 源码里即为缺陷。
_INTERVAL_AND_BATCH_FIELDS: Final = (
    "poll_review_outbound_seconds",
    "poll_send_seconds",
    "poll_fetch_inbound_seconds",
    "poll_review_inbound_seconds",
    "poll_cleanup_seconds",
    "batch_size",
)

Tick = Callable[[], Awaitable[object]]


def _repository_root() -> Path:
    """锚定真实仓库根：从本文件向上找到 ``.git`` 所在目录。

    不能按固定层数回推：mutmut 把工程复制到 ``mutants/`` 下运行，固定层数会落到
    ``mutants/``，AST 扫描对象就变成变异体。口径与理由同
    ``tests/unit/test_repository_statements.py`` 的同名辅助函数。
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").exists():
            return parent
    raise AssertionError("测试不在仓库内运行：向上未找到 .git")


_FLOWS_DIR: Final = _repository_root() / "src" / "vs_email_forwarding" / "flows"


async def _noop_tick() -> None:
    """占位 tick：调度器只要求「零参、可 await」，本文件不驱动循环。"""


def _ticks() -> dict[str, Tick]:
    """五条任务各给一个占位 tick（调度器要求五条齐全才构造）。"""
    return {name: _noop_tick for name in _EXPECTED_TASK_NAMES}


def _settings_with(monkeypatch: pytest.MonkeyPatch, **overrides: int) -> Settings:
    """构造只认用例显式给值的配置对象。

    ``_env_file=None`` 只关掉 ``.env`` 读取；pydantic-settings 的优先级是「init 参数 >
    环境变量 > 点文件 > 声明默认值」，导出在 shell 里的 ``POLL_CLEANUP_SECONDS=7200``
    这类值仍会盖掉默认值。因此这里再把这六个键逐个从环境里剔除，让用例只认自己写进去的
    取值（`docs/testing.md` R12：不依赖全局状态）。
    """
    for field in _INTERVAL_AND_BATCH_FIELDS:
        monkeypatch.delenv(field.upper(), raising=False)
    return Settings(_env_file=None, **overrides)


def _declared_defaults() -> dict[str, int]:
    """读出上列字段在配置声明里的默认值（期望值出处是 `config.py` 的声明本身）。"""
    return {name: Settings.model_fields[name].default for name in _INTERVAL_AND_BATCH_FIELDS}


def _flows_sources() -> dict[str, str]:
    """读出 ``flows/`` 下全部模块源码（``.git`` 锚定，mutmut 环境下不指向 ``mutants/``）。"""
    sources = {
        path.name: path.read_text(encoding="utf-8") for path in sorted(_FLOWS_DIR.glob("*.py"))
    }
    for required in ("scheduler.py", "cleanup.py"):
        assert required in sources, f"扫描面缺少 flows/{required}：本卡的核心模块没落到仓库里"
    return sources


def _int_literals(source: str, *, filename: str) -> list[tuple[int, int]]:
    """列出源码里全部整数常量（布尔量不算），返回 ``(取值, 行号)``。"""
    tree = ast.parse(source, filename=filename)
    return [
        (node.value, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        # 布尔量也是 int 的子类，但不是「间隔 / 批量的字面量」。
        and not isinstance(node.value, bool)
    ]


class TestIntervalComesFromConfig:
    """`AC-23`：间隔从配置读，改配置后重建即为新值；五条任务一条不少。"""

    def test_interval_comes_from_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 五个值彼此不同且都偏离默认值：任何「写死默认值」的实现都会红。
        configured = {
            "poll_review_outbound_seconds": 17,
            "poll_send_seconds": 23,
            "poll_fetch_inbound_seconds": 31,
            "poll_review_inbound_seconds": 41,
            "poll_cleanup_seconds": 3607,
        }
        scheduler = Scheduler(_settings_with(monkeypatch, **configured), _ticks())

        for task_name, field_name in _TASK_INTERVAL_FIELDS.items():
            assert scheduler.interval_seconds(task_name) == configured[field_name], (
                f"{task_name} 的间隔没有取自配置项 {field_name}"
            )

        # 改配置后**重新构造**即为新值（AC-23 的改写口径：不测墙钟，只测构造读到的值）。
        rebuilt = Scheduler(_settings_with(monkeypatch, poll_send_seconds=29), _ticks())
        assert rebuilt.interval_seconds("send_email") == 29
        # 先构造的那个不受影响：间隔是构造时读入的，不是每次现读。
        assert scheduler.interval_seconds("send_email") == 23

    def test_default_intervals_are_the_documented_ones(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 不传任何覆盖时，读到的是配置声明的默认值——期望值来自 NFR-2 与 design.md §7。
        scheduler = Scheduler(_settings_with(monkeypatch), _ticks())

        assert {
            task_name: scheduler.interval_seconds(task_name) for task_name in _EXPECTED_TASK_NAMES
        } == {
            "review_outbound": 10,
            "send_email": 10,
            "fetch_inbound": 10,
            "review_inbound": 10,
            "cleanup": 3600,
        }

    def test_default_intervals_ignore_exported_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # _settings_with 的隔离要能反向证明：把六个键都导出到环境里（值刻意取成与默认值
        # 不同的 7200），默认值用例仍须读到声明默认值——隔离一旦失效，这条会红。
        for field in _INTERVAL_AND_BATCH_FIELDS:
            monkeypatch.setenv(field.upper(), "7200")

        scheduler = Scheduler(_settings_with(monkeypatch), _ticks())

        assert scheduler.interval_seconds("cleanup") == 3600

    def test_incomplete_task_set_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # design.md §3「后台任务共五条」：少一条要当场报错，不能静默只调度四条。
        incomplete = _ticks()
        del incomplete["cleanup"]

        with pytest.raises(ValueError, match="cleanup"):
            Scheduler(_settings_with(monkeypatch), incomplete)

    def test_unknown_task_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 另一侧边界：多传一个不认识的任务名同样要当场报错，不能静默忽略（docstring 里
        # 「缺一条或名字不认识都当场报错」的「多」侧由此有失败证据）。
        ticks = _ticks()
        ticks["bogus"] = _noop_tick

        with pytest.raises(ValueError, match="bogus"):
            Scheduler(_settings_with(monkeypatch), ticks)


class TestNoIntervalLiterals:
    """`NFR-2` / `AGENTS.md` §8.2：间隔与批量不得以字面量出现在 `flows/` 源码里。"""

    def test_interval_and_batch_defaults_match_spec(self) -> None:
        # 扫描的禁止集合取自配置声明，其取值必须与规格写明的默认值一致——否则集合
        # 会随实现漂移，扫描就变成空转。
        assert _declared_defaults() == _DOCUMENTED_DEFAULTS

    def test_no_interval_literals(self) -> None:
        forbidden = _declared_defaults()
        # 几个字段可能共享同一个默认值（四条业务流的间隔都是 10），失败信息要把它们列全，
        # 否则定位时会被误导成「只有某一个字段被写死」。
        fields_by_value: dict[int, list[str]] = {}
        for name, value in forbidden.items():
            fields_by_value.setdefault(value, []).append(name)

        offenders: list[str] = []
        for filename, source in _flows_sources().items():
            for value, lineno in _int_literals(source, filename=filename):
                if value in fields_by_value:
                    fields = "/".join(fields_by_value[value])
                    offenders.append(
                        f"flows/{filename}:{lineno} 出现字面量 {value}"
                        f"（等于配置项 {fields} 的取值）"
                    )

        assert not offenders, (
            "间隔与批量必须以配置项读取，不得写成字面量（NFR-2 / AGENTS.md §8.2）："
            + "；".join(offenders)
        )
