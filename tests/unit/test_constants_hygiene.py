#!/usr/bin/env python3
"""常量词表的位置下标守护与「具名取值」独立取值校验（spec 0001 / T-46 ①、T-63 裁决一）。

`constants.py` 的六族词表（方向、正文格式、错误分类、拒审来源、丢弃原因、消息状态）各有
具名常量，消费侧一律取名字。位置下标（`DIRECTIONS[0]` 这种写法）与**数字切片**
（`MESSAGE_STATES[3:]`）都会在词表中间插入一项时一起指错，而 ruff / mypy 都查不出来——
所以把它钉成可执行的检查：

* 真真切扫描 `src/vs_email_forwarding/` 下全部 `.py`，断言零「词表 + 数字下标 / 切片」；
  切片（`ast.Slice`）与负下标（`ast.UnaryOp(USub)`）都算违规：前者表达「第几项到第几项」、
  后者表达「倒数第几项」，两者在词表增删时会静默指错；
* 六族词表必须**由名字拼出**、`DEFAULT_BODY_FORMAT` 必须引用名字（结构断言，靠等值断言
  是抓不住的——重新写回字面量同样「相等」）；
* 具名常量的**取值**必须与设计原文写死的独立表逐项相等（`_DISCARD_REASON_LITERALS` /
  `_FAMILY_LITERALS`）：用被测元组当 oracle 只能证明「集合等于自己」，对「同族内换值」
  恒绿（`docs/testing.md` R2）；
* 扫描器自己会咬：合成源码的违规行号 == 手写期望（反恒绿，口径同 `test_config_hygiene.py`）。

期望值出处：设计原文写死在本文件的表里（`design.md` §2.2 / §2.3 / §2.4 / §3.2 / §3.4 的
受控枚举），不引用 `constants` 的任何元组或下标。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

import vs_email_forwarding.constants as constants

#: 六族词表的模块级名字（`constants.py` 的导出名）：凡对这些名字做整数下标或数字切片
#: 即违规。
_VOCABULARIES: Final = (
    "MESSAGE_STATES",
    "MESSAGE_DELIVERY_STATES",
    "DIRECTIONS",
    "BODY_FORMATS",
    "ERROR_CLASSES",
    "REJECT_SOURCES",
    "DISCARD_REASONS",
    "STATUSES",
    "OUTCOMES",
)

#: 11 类丢弃原因的「常量名 → 字面值」独立对照表。
#:
#: **出处**：`specs/0001-email-forwarding-service/design.md` §3.4 的十一类枚举原文，逐字抄写。
#: 这张表是本用例唯一的 oracle：它**不引用** `constants.DISCARD_REASONS`、也不按下标取元组，
#: 因此「把 `unparsable` 改成 `mdn`」这类同族换值（元组自比恒绿）必须让它变红。
_DISCARD_REASON_LITERALS: Final = (
    ("DISCARD_REASON_NO_REFERENCE", "no_reference"),
    ("DISCARD_REASON_NO_MATCH", "no_match"),
    ("DISCARD_REASON_MDN", "mdn"),
    ("DISCARD_REASON_DSN_WITHOUT_ID", "dsn_without_id"),
    ("DISCARD_REASON_DSN_TARGET_GONE", "dsn_target_gone"),
    ("DISCARD_REASON_DSN_TARGET_NOT_SENT", "dsn_target_not_sent"),
    ("DISCARD_REASON_DUPLICATE", "duplicate"),
    ("DISCARD_REASON_NO_REPLY_FROM", "no_reply_from"),
    ("DISCARD_REASON_MALFORMED_REFERENCE", "malformed_reference"),
    ("DISCARD_REASON_REPLY_TARGET_GONE", "reply_target_gone"),
    ("DISCARD_REASON_UNPARSABLE", "unparsable"),
)

#: 具名常量的前缀 → 该族「**常量名 → 字面值**」的独立对照表（出处：设计原文，不引用被测元组）。
#:
#: 写成**名字 → 字面值**而不是「前缀 → 取值集合」是必须的：集合比较对「同族内互换两个常量
#: 的取值」恒绿（`DIRECTION_OUTBOUND = "inbound"` / `DIRECTION_INBOUND = "outbound"` 的集合
#: 仍是 `{outbound, inbound}`），只有逐个名字比对才抓得到（T-63 F3-①）。
#: 前缀与词表名不互相包含（`DIRECTION_` 不匹配 `DIRECTIONS`）。
_FAMILY_LITERALS: Final = (
    (
        "MESSAGE_STATE_",
        (
            ("MESSAGE_STATE_SUBMITTED", "submitted"),
            ("MESSAGE_STATE_REVIEWING", "reviewing"),
            ("MESSAGE_STATE_REJECTED", "rejected"),
            ("MESSAGE_STATE_SENDING", "sending"),
            ("MESSAGE_STATE_SENT", "sent"),
            ("MESSAGE_STATE_FAILED", "failed"),
        ),
    ),
    (
        "DIRECTION_",
        (
            ("DIRECTION_OUTBOUND", "outbound"),
            ("DIRECTION_INBOUND", "inbound"),
        ),
    ),
    (
        "BODY_FORMAT_",
        (
            ("BODY_FORMAT_TEXT", "text"),
            ("BODY_FORMAT_HTML", "html"),
        ),
    ),
    (
        "ERROR_CLASS_",
        (
            ("ERROR_CLASS_PERMANENT", "permanent"),
            ("ERROR_CLASS_TRANSIENT", "transient"),
            ("ERROR_CLASS_INTERNAL", "internal"),
        ),
    ),
    (
        "REJECT_SOURCE_",
        (
            ("REJECT_SOURCE_REVIEW_REJECTED", "review_rejected"),
            ("REJECT_SOURCE_REVIEW_ERROR", "review_error"),
            ("REJECT_SOURCE_REVIEW_INCOMPLETE", "review_incomplete"),
        ),
    ),
    ("DISCARD_REASON_", _DISCARD_REASON_LITERALS),
)


def _repository_root() -> Path:
    """锚定真实仓库根：从本文件向上找到 `.git` 所在目录。

    不能按固定层数回推：mutmut 把工程复制到 `mutants/` 下运行，固定层数会落到 `mutants/`，
    扫描对象就变成变异体。口径与理由同 `tests/unit/test_config_hygiene.py`。
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").exists():
            return parent
    raise AssertionError("测试不在仓库内运行：向上未找到 .git")


_SRC_DIR: Final = _repository_root() / "src" / "vs_email_forwarding"


def _sources() -> dict[str, str]:
    """`src/vs_email_forwarding/` 下的全部 `.py` 源码，键是相对该目录的 POSIX 路径。"""
    return {
        path.relative_to(_SRC_DIR).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(_SRC_DIR.rglob("*.py"))
    }


def _is_integer_bound(node: ast.expr | None) -> bool:
    """该切片端点是否是整数（含负整数：AST 里 `-1` 是 `UnaryOp(USub)`，不是负常量）。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return True
    return (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, int)
    )


def _is_integer_position(index: ast.expr) -> bool:
    """该下标是否是整数位置**或数字切片**。

    负下标与数字切片正是本检查最想抓的两类——`DISCARD_REASONS[-1]` 与 `MESSAGE_STATES[3:]`
    都会在词表追加或中间插入一项后静默指错，而 `mutmut` / `ruff` / `mypy` 都不会因此报错。
    `_VOCABULARIES[:]` 这类两端留空的切片不表达「第几项」，不算违规。
    """
    if isinstance(index, ast.Slice):
        return _is_integer_bound(index.lower) or _is_integer_bound(index.upper)
    return _is_integer_bound(index)


def _positional_lookup_lines(source: str, *, filename: str) -> list[int]:
    """列出「词表 + 整数下标 / 数字切片」的位置，每处一个行号（非常量下标不算）。"""
    hits: list[int] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not isinstance(node, ast.Subscript) or not isinstance(node.value, ast.Name):
            continue
        if node.value.id not in _VOCABULARIES:
            continue
        if _is_integer_position(node.slice):
            hits.append(node.lineno)
    return sorted(hits)


def _module_assignments(source: str, *, filename: str) -> dict[str, ast.expr]:
    """模块级赋值的「名字 → 右值表达式」（含 `AnnAssign`，供结构断言用）。"""
    assignments: dict[str, ast.expr] = {}
    for node in ast.parse(source, filename=filename).body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                assignments[node.target.id] = node.value
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = node.value
    return assignments


def test_scan_face_is_not_empty() -> None:
    """扫描面必须真读到源码：源文件没读到时，「没有下标」是空承诺。"""
    sources = _sources()
    assert "constants.py" in sources
    assert len(sources) >= 15


def test_no_positional_vocabulary_lookup_in_src() -> None:
    """`src/` 里零处「按下标或数字切片取词表取值」，一律用具名常量（T-46 ① / T-63 裁决一）。"""
    offenders = {
        name: lines
        for name, source in _sources().items()
        if (lines := _positional_lookup_lines(source, filename=name))
    }
    assert offenders == {}


def test_every_vocabulary_member_has_a_named_constant() -> None:
    """六族每个取值都有导出的具名常量，且名字与取值逐项对上设计原文。

    oracle 是 `_FAMILY_LITERALS`（设计原文写死的「名字 → 字面值」表），**不是**被测元组：
    集合比较对「同族内互换取值」恒绿（T-63 F3-①），所以这里逐个名字断言取值，再单独断言
    名字集合不重不漏。
    """
    for prefix, entries in _FAMILY_LITERALS:
        for name, literal in entries:
            assert getattr(constants, name) == literal, f"{name} 不等于设计原文的 {literal!r}"
        # 只挑**取值为字符串**的导出名：`MESSAGE_STATE_LABELS`（取值映射）与
        # `MESSAGE_STATE_TRANSITIONS`（迁移表）与六态常量共用前缀，但它们不是「取值常量」，
        # 不参与「名字 ↔ 取值」一一对应；名字集合这条断言保证不重不漏。
        names = [
            name
            for name in constants.__all__
            if name.startswith(prefix) and isinstance(getattr(constants, name), str)
        ]
        assert sorted(names) == sorted(name for name, _ in entries), (
            f"{prefix}* 的具名常量名字集合与设计原文对照表不一致：{sorted(names)!r}"
        )


def test_discard_reason_constants_equal_design_literals() -> None:
    """11 个丢弃原因常量逐项等于 design.md §3.4 原文的字面值（独立 oracle，T-63 F2）。

    `constants.DISCARD_REASONS` 与 `DISCARD_REASON_*` 互相印证是恒绿的：把任一常量改成
    **同族另一取值**（`"unparsable"` → `"mdn"`）时元组与名字集合同时跟着变，只有这张与设计
    原文对齐的表能抓到。顺序也一并钉住：事件里十一类按 `DISCARD_REASONS` 的顺序落日志。
    """
    assert len(_DISCARD_REASON_LITERALS) == 11
    for name, literal in _DISCARD_REASON_LITERALS:
        assert getattr(constants, name) == literal, f"{name} 不等于设计原文的 {literal!r}"
    assert tuple(literal for _, literal in _DISCARD_REASON_LITERALS) == constants.DISCARD_REASONS, (
        "DISCARD_REASONS 的取值或顺序与 design.md §3.4 的枚举原文不一致"
    )


def test_vocabulary_tuples_are_built_from_names() -> None:
    """六族词表由名字拼出、`DEFAULT_BODY_FORMAT` 引用名字（单一真源，T-46 ①）。"""
    assigned = _module_assignments(_sources()["constants.py"], filename="constants.py")
    for vocabulary in (
        "MESSAGE_STATES",
        "MESSAGE_DELIVERY_STATES",
        "DIRECTIONS",
        "BODY_FORMATS",
        "ERROR_CLASSES",
        "REJECT_SOURCES",
        "DISCARD_REASONS",
    ):
        value = assigned[vocabulary]
        assert isinstance(value, ast.Tuple), f"{vocabulary} 不再是元组字面拼接"
        assert value.elts, f"{vocabulary} 是空元组"
        assert all(isinstance(element, ast.Name) for element in value.elts), (
            f"{vocabulary} 里仍有字面量：应当逐个引用具名常量"
        )
    assert isinstance(assigned["DEFAULT_BODY_FORMAT"], ast.Name)


def test_scanner_flags_synthetic_violations() -> None:
    """扫描器自己会咬：合成源码的违规行号 == 手写期望（反恒绿）。

    切片（第 5 行起）与负下标（第 3 行）都在咬的范围内：两者都表达「第几项」，都会在词表
    增删时静默指错（T-63 裁决一要求把数字切片纳入守护）。
    """
    synthetic = (
        "import something\n"
        "first = DIRECTIONS[0]\n"
        "second = ERROR_CLASSES[-1]\n"
        "third = DISCARD_REASONS[9]\n"
        "slice_tail = MESSAGE_STATES[3:]\n"
        "slice_head = BODY_FORMATS[:2]\n"
        "slice_negative = REJECT_SOURCES[-2:]\n"
        "slice_all_is_not_a_position = STATUSES[:]\n"
        "other_name = SOMETHING_ELSE[0]\n"
        "non_literal = REJECT_SOURCES[index]\n"
        "fourth = STATUSES[1]\n"
        "fifth = OUTCOMES[2]\n"
    )
    assert _positional_lookup_lines(synthetic, filename="synthetic.py") == [
        2,
        3,
        4,
        5,
        6,
        7,
        11,
        12,
    ]
