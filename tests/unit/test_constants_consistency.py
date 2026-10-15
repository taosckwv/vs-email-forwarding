#!/usr/bin/env python3
"""三方取值一致与共享叶子层的静态守护（spec 0001 / T-38 / `NFR-4` / `AC-26` 的常量面）。

真源（期望值全部来自这三处，**不来自被测代码**）：

* `docs/api-contract.yaml` 的 `MessageState` / `BodyFormat` 枚举与 `x-state-labels`——
  对外契约的唯一真源（`design.md` §4.1）；
* `specs/0001-email-forwarding-service/design.md` §2.3（六态状态机与各状态的中文名）、
  §2.4（错误分类 `permanent` / `transient` / `internal`）、§2.2（拒审来源）、§2.6（正文格式）、
  §3.4（`received` 是入站终态）——本文件把设计原文里的**字面取值**写死在期望里，
  再让 `constants.py`、迁移文件与契约各自对上；
* `migrations/versions/*.py` 里 `CheckConstraint` 的 `IN (...)` 文本（静态读源码，不连库：
  `tests/unit/` 不依赖数据库，`docs/testing.md` §4.1）。

为什么第三个源是**迁移文件**而不是 `db/models.py`：`db/models.py` 的 `CHECK` 取值由
`constants` 推出（同一处代码），拿它跟 `constants` 比是同义反复；手写 SQL 的迁移文件才是
独立的一份说法。`db/models.py` 那一侧由既有的 `tests/unit/test_models_enum_checks.py` 覆盖
（编译产物里的取值集合），本文件不重复。

另两条硬约束的静态判定（`docs/architecture.md` 硬规则第 4 条、`AGENTS.md` §7）：

* `constants.py` 是共享叶子层——**零项目内 import**；
* 时间格式 `TIMESTAMP_FORMAT` 与契约声明的「UTC RFC 3339 固定六位小数 + `Z`」一致
  （拿契约里给的例子反过来喂给该格式解析，而不是拿格式自己格式化再断言自己）。
"""

from __future__ import annotations

import ast
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import yaml

from vs_email_forwarding import constants

_REPO_ROOT: Final = Path(__file__).resolve().parents[2]
_CONTRACT_PATH: Final = _REPO_ROOT / "docs" / "api-contract.yaml"
_CONSTANTS_PATH: Final = _REPO_ROOT / "src" / "vs_email_forwarding" / "constants.py"
_MIGRATIONS_DIR: Final = _REPO_ROOT / "migrations" / "versions"

#: 设计原文里的字面取值（`design.md` §2.3 的六态 + §3.4 的入站终态 `received`）。
_DESIGN_MESSAGE_STATES: Final = frozenset(
    {"submitted", "reviewing", "rejected", "sending", "sent", "failed"}
)
_DESIGN_RECEIVED_STATE: Final = "received"
_DESIGN_MESSAGE_STATES_IN_MESSAGES_TABLE: Final = frozenset({"sending", "sent", "failed"})

#: `design.md` §2.6（正文格式）、§2.3（方向）、§2.2（拒审来源）、§2.4（错误分类）的字面取值。
_DESIGN_BODY_FORMATS: Final = frozenset({"text", "html"})
_DESIGN_DIRECTIONS: Final = frozenset({"outbound", "inbound"})
_DESIGN_REJECT_SOURCES: Final = frozenset({"review_rejected", "review_error", "review_incomplete"})
_DESIGN_ERROR_CLASSES: Final = frozenset({"permanent", "transient", "internal"})

#: 迁移里 `CHECK` 的形态：`<列> IN ('a', 'b')`（可为 `IS NULL OR <列> IN (...)` 的一半）。
_CHECK_IN_PATTERN: Final = re.compile(r"(\w+)\s+IN\s*\(([^)]*)\)")
_QUOTED_VALUE_PATTERN: Final = re.compile(r"'([^']*)'")

#: 契约 `Timestamp` 描述里给出的示例（`docs/api-contract.yaml`）。
_TIMESTAMP_EXAMPLE_PATTERN: Final = re.compile(r"`(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)`")

_ProjectImport: Final = frozenset({"vs_email_forwarding"})


def _contract() -> dict[str, Any]:
    """读契约真源（UTF-8；YAML 解析失败时在这里先红，而不是让下面的断言悄悄跳过）。"""
    return yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))


def _contract_schema(name: str) -> dict[str, Any]:
    schema = _contract()["components"]["schemas"][name]
    assert isinstance(schema, dict), f"契约里 {name} 不是对象 schema"
    return schema


def _contract_enum(schema_name: str) -> set[str]:
    """契约里某个枚举 schema 的取值集合；schema 退化成没有 `enum` 时在这里先红。"""
    values = _contract_schema(schema_name).get("enum")
    assert isinstance(values, list) and values, f"契约 schema {schema_name} 没有 enum"
    return {str(value) for value in values}


def _check_value_lists() -> dict[str, set[str]]:
    """迁移文件里「列 → 该列全部 `IN (...)` 取值」的并集（静态读字符串，不连库）。

    同列可能出现在多条 `CHECK` 里（整列取值集合 + 分支约束，如 `messages.status` 与
    `direction = 'outbound' AND status IN (...)`），这里按并集取——并集必须恰好等于设计
    声明的取值集合，多一个少一个都在下面的断言里暴露。
    """
    found: dict[str, set[str]] = {}
    files = sorted(_MIGRATIONS_DIR.glob("*.py"))
    assert files, "迁移目录下没有 .py 文件：扫描面为空时「取值一致」是空承诺"
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            for match in _CHECK_IN_PATTERN.finditer(node.value):
                column = match.group(1)
                values = set(_QUOTED_VALUE_PATTERN.findall(match.group(2)))
                if values:
                    found.setdefault(column, set()).update(values)
    return found


def _constants_imports() -> list[ast.Import | ast.ImportFrom]:
    tree = ast.parse(_CONSTANTS_PATH.read_text(encoding="utf-8"), filename=str(_CONSTANTS_PATH))
    return [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]


def test_contract_scan_face_is_not_empty() -> None:
    """扫描面必须真读到三份产物：任一份没读到时，后面的「一致」都是空承诺。"""
    contract = _contract()
    assert set(contract["paths"]) >= {"/v1/messages", "/v1/messages/{id}"}
    assert _CONSTANTS_PATH.is_file() and "MESSAGE_STATES" in _CONSTANTS_PATH.read_text(
        encoding="utf-8"
    )
    assert _check_value_lists(), "迁移里没扫到任何 CHECK 取值"
    assert _constants_imports(), "constants.py 里没扫到任何 import 语句"


def test_message_state_three_way_consistency() -> None:
    """六态：契约枚举 == `constants.MESSAGE_STATES` == 设计 §2.3 的字面取值（三方一致）。"""
    contract_states = _contract_enum("MessageState")
    assert contract_states == _DESIGN_MESSAGE_STATES
    assert set(constants.MESSAGE_STATES) == _DESIGN_MESSAGE_STATES
    assert [str(value) for value in constants.MESSAGE_STATES] == [
        str(value) for value in _contract_schema("MessageState")["enum"]
    ]


def test_state_labels_cover_exactly_the_six_states() -> None:
    """根级 `x-state-labels` 的键就是那六态（中文名与取值一一对应，没有第七个）。"""
    labels = _contract()["x-state-labels"]
    assert set(labels) == _DESIGN_MESSAGE_STATES
    # 标签是给人看的中文名：值必须是字符串，且与 constants 的标签表指向同一批键。
    assert all(isinstance(text, str) and text for text in labels.values())
    assert set(constants.MESSAGE_STATE_LABELS) == _DESIGN_MESSAGE_STATES


def test_body_format_three_way_consistency() -> None:
    """正文格式：契约枚举 == `constants.BODY_FORMATS` == 设计 §2.6 的字面取值。"""
    assert _contract_enum("BodyFormat") == _DESIGN_BODY_FORMATS
    assert set(constants.BODY_FORMATS) == _DESIGN_BODY_FORMATS


def test_migration_check_values_match_design_and_constants() -> None:
    """迁移 `CHECK` 的取值集合：与设计字面取值、`constants` 的取值集合三方一致。"""
    checks = _check_value_lists()
    assert checks["direction"] == _DESIGN_DIRECTIONS
    assert checks["body_format"] == _DESIGN_BODY_FORMATS
    assert checks["source"] == _DESIGN_REJECT_SOURCES
    assert checks["last_error_class"] == _DESIGN_ERROR_CLASSES
    # `messages.status`：出站三态 + 入站终态 `received`（design.md §2.2 / §2.3、§3.4）。
    expected_status = _DESIGN_MESSAGE_STATES_IN_MESSAGES_TABLE | {_DESIGN_RECEIVED_STATE}
    assert checks["status"] == expected_status
    # 与 constants 对齐的那一半：迁移里不得出现 constants 之外的取值。
    assert set(constants.DIRECTIONS) == _DESIGN_DIRECTIONS
    assert set(constants.REJECT_SOURCES) == _DESIGN_REJECT_SOURCES
    assert set(constants.ERROR_CLASSES) == _DESIGN_ERROR_CLASSES
    assert constants.RECEIVED_STATE == _DESIGN_RECEIVED_STATE
    assert checks["status"] <= set(constants.MESSAGE_STATES) | {constants.RECEIVED_STATE}


def test_constants_module_imports_only_stdlib() -> None:
    """`constants.py` 是共享叶子层：零项目内 import，且不引第三方库（architecture 硬规则 4）。"""
    for node in _constants_imports():
        module = node.module if isinstance(node, ast.ImportFrom) else node.names[0].name
        assert module, f"无法判定 import 的来源：{ast.dump(node)[:80]}"
        root = module.split(".")[0]
        assert root not in _ProjectImport, f"constants.py 不得 import 项目内模块：{module}"
        assert root in sys.stdlib_module_names, f"constants.py 只允许标准库：{module}"


def test_timestamp_format_round_trips_the_contract_example() -> None:
    """契约里 `Timestamp` 给的示例必须能被 `constants.TIMESTAMP_FORMAT` 解析（同一形状）。"""
    description = _contract_schema("Timestamp")["description"]
    example = _TIMESTAMP_EXAMPLE_PATTERN.search(description)
    assert example is not None, "契约 Timestamp 描述里没有 RFC 3339 示例，期望值失去锚点"
    text = example.group(1)
    assert datetime.strptime(text, constants.TIMESTAMP_FORMAT).year == int(text[:4])
    # 反向：按该格式格式化出来的串也必须是「六位小数 + Z」的形状（不是五位、不带偏移）。
    rendered = datetime(2030, 1, 1, tzinfo=UTC).strftime(constants.TIMESTAMP_FORMAT)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", rendered)
    assert text.endswith("Z") and not text.endswith("+00:00")
