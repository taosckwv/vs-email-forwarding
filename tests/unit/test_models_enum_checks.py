#!/usr/bin/env python3
"""受控枚举 `CHECK` 的**语义**：取值集合与 `NULL` 分支都必须真的写在约束里。

`tests/unit/test_models_metadata.py` 只看 `CHECK` 的条数、命名与涉及列。于是「表达式被削弱」
这类破坏（少一半分支、约束退化成没有表达式、取值集合被换掉或列名被写成 `None`）在当时不会让
任何断言变红——DDL 照建，库里却不再声明原本的取值约束。本文件补的正是这层语义。

断言口径见 `docs/testing.md` §4.5：**只看表达式结构与编译产物里的结构片段**（列名、取值集合、
子句形状），不与手抄的整段 SQL 文本比相等。

分工：跨「契约 `enum` / `constants` / 迁移 `CHECK`」的取值一致性由 `plan.md` §7.1 的
`tests/unit/test_constants_consistency.py` 负责（尚未落地）；本文件只锁「约束表达式里确实带着
这份取值集合、且 `NULL` 分支没有被削掉」这一层。

期望值来源（`docs/testing.md` R2，不得拿被测模块生成期望值）：

* 哪些列是受控枚举、哪些可为空：`design.md` §2.1 的表与字段清单；
* 取值集合：运行期唯一真源 `vs_email_forwarding.constants`。`db/models.py` 里那份字面量是为
  DDL 自洽而存在的第二份文本，它属**被测模块**，不能当期望值来源。
"""

from __future__ import annotations

from typing import Final

import pytest
from sqlalchemy import CheckConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BinaryExpression, BooleanClauseList, ClauseElement

from vs_email_forwarding import constants
from vs_email_forwarding.db import models

#: `design.md` §2.1：非空受控枚举列 -> (物理表, 列, `CHECK` 名, 取值集合)。
_ENUM_CHECKS: Final[tuple[tuple[str, str, str, tuple[str, ...]], ...]] = (
    ("pending_messages", "direction", "ck_pending_messages_direction_values", constants.DIRECTIONS),
    (
        "pending_messages",
        "body_format",
        "ck_pending_messages_body_format_values",
        constants.BODY_FORMATS,
    ),
    ("messages", "direction", "ck_messages_direction_values", constants.DIRECTIONS),
    ("messages", "status", "ck_messages_status_values", constants.MESSAGES_STATUS_VALUES),
    ("messages", "body_format", "ck_messages_body_format_values", constants.BODY_FORMATS),
    (
        "rejected_messages",
        "direction",
        "ck_rejected_messages_direction_values",
        constants.DIRECTIONS,
    ),
    ("rejected_messages", "source", "ck_rejected_messages_source_values", constants.REJECT_SOURCES),
)

#: `design.md` §2.1：可为空的受控枚举列（`last_error_class`：「可取 NULL，非 NULL 时受取值约束」）。
_NULLABLE_ENUM_CHECKS: Final[tuple[tuple[str, str, str, tuple[str, ...]], ...]] = (
    (
        "pending_messages",
        "last_error_class",
        "ck_pending_messages_last_error_class_values",
        constants.ERROR_CLASSES,
    ),
    (
        "messages",
        "last_error_class",
        "ck_messages_last_error_class_values",
        constants.ERROR_CLASSES,
    ),
    (
        "rejected_messages",
        "last_error_class",
        "ck_rejected_messages_last_error_class_values",
        constants.ERROR_CLASSES,
    ),
)

_ALL_ENUM_CHECKS: Final = (*_ENUM_CHECKS, *_NULLABLE_ENUM_CHECKS)


def _check(table_name: str, check_name: str) -> CheckConstraint:
    """按名字取出约束；约束被改名 / 删除时在这里先红，而不是让后面的断言悄悄跳过。"""
    for constraint in models.metadata.tables[table_name].constraints:
        if isinstance(constraint, CheckConstraint) and constraint.name == check_name:
            return constraint
    raise AssertionError(f"{table_name} 里没有 {check_name}")


def _clause(check: CheckConstraint) -> ClauseElement:
    """`CHECK` 的表达式树；约束退化成「没有表达式」时在这里先红。"""
    sqltext = check.sqltext
    assert isinstance(sqltext, ClauseElement), (
        f"{check.name} 没有可判定的 CHECK 表达式：{sqltext!r}"
    )
    return sqltext


def _compiled(check: CheckConstraint) -> str:
    """表达式按 PostgreSQL 方言编译出的文本（与 DDL 里 `CHECK (...)` 的内容一致）。"""
    return str(
        _clause(check).compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


def _value_set_sql(values: tuple[str, ...]) -> str:
    """取值集合在编译产物里的写法，由 `constants` 的取值推出（片段，不是整段 SQL）。"""
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"


def _is_in_predicate(node: object, column_name: str) -> bool:
    return (
        isinstance(node, BinaryExpression)
        and node.operator is operators.in_op
        and getattr(node.left, "name", None) == column_name
    )


def _is_null_predicate(node: object, column_name: str) -> bool:
    return (
        isinstance(node, BinaryExpression)
        and node.operator is operators.is_
        and getattr(node.left, "name", None) == column_name
    )


@pytest.mark.parametrize(
    ("table_name", "column_name", "check_name", "values"),
    _ENUM_CHECKS,
    ids=[f"{table}.{column}" for table, column, _, _ in _ENUM_CHECKS],
)
def test_enum_check_restricts_column_to_expected_values(
    table_name: str, column_name: str, check_name: str, values: tuple[str, ...]
) -> None:
    """非空枚举列的 `CHECK` 必须恰好是「该列 IN 取值集合」这一条谓词。"""
    clause = _clause(_check(table_name, check_name))

    assert _is_in_predicate(clause, column_name), f"{check_name} 不是单条 IN 谓词：{clause!r}"
    assert clause.right.value == list(values), f"{check_name} 的取值集合变了：{clause!r}"


@pytest.mark.parametrize(
    ("table_name", "column_name", "check_name", "values"),
    _NULLABLE_ENUM_CHECKS,
    ids=[f"{table}.{column}" for table, column, _, _ in _NULLABLE_ENUM_CHECKS],
)
def test_nullable_enum_check_is_null_or_expected_values(
    table_name: str, column_name: str, check_name: str, values: tuple[str, ...]
) -> None:
    """可为空枚举列的 `CHECK` 必须是「该列 IS NULL」或「该列 IN 取值集合」两半，缺一不可。"""
    clause = _clause(_check(table_name, check_name))

    assert isinstance(clause, BooleanClauseList), f"{check_name} 不是 OR 串联：{clause!r}"
    assert clause.operator is operators.or_, f"{check_name} 不是 OR：{clause!r}"
    branches = list(clause.get_children())
    assert len(branches) == 2, f"{check_name} 的分支数不是 2：{clause!r}"

    null_branches = [branch for branch in branches if _is_null_predicate(branch, column_name)]
    value_branches = [branch for branch in branches if _is_in_predicate(branch, column_name)]
    assert len(null_branches) == 1, f"{check_name} 少了「{column_name} IS NULL」这一半：{clause!r}"
    assert len(value_branches) == 1, f"{check_name} 少了 IN 取值集合这一半：{clause!r}"
    assert value_branches[0].right.value == list(values), f"{check_name} 的取值集合变了：{clause!r}"


@pytest.mark.parametrize(
    ("table_name", "column_name", "check_name", "values"),
    _ALL_ENUM_CHECKS,
    ids=[f"{table}.{column}" for table, column, _, _ in _ALL_ENUM_CHECKS],
)
def test_enum_check_compiles_declared_halves(
    table_name: str, column_name: str, check_name: str, values: tuple[str, ...]
) -> None:
    """真正进 DDL 的编译产物里，两半都必须以结构片段的形式在。"""
    compiled = _compiled(_check(table_name, check_name))

    assert f"{column_name} IN {_value_set_sql(values)}" in compiled, compiled
    if (table_name, column_name, check_name, values) in _NULLABLE_ENUM_CHECKS:
        assert f"{column_name} IS NULL" in compiled, compiled
        assert " OR " in compiled, compiled
    else:
        assert " IS NULL" not in compiled, compiled
        assert " OR " not in compiled, compiled
