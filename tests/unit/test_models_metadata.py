"""`db/models.py` 的元数据形状：四张表、列与可空性、索引、自引用外键、`CHECK` 清单。

**不连数据库**：只读 SQLAlchemy 的内存元数据（`models.metadata`），因此属 `tests/unit/`
（`docs/testing.md` §3.1）。真实 PostgreSQL 上的约束语义（唯一键真的挡住重复入站、级联
删除真的清回复）留给 `tests/integration/`（`plan.md` §7.1）。

期望值来源（`docs/testing.md` R2，逐项都直取规格文本，不从被测模块推导）：

* 列清单与可空性、受控枚举列、索引与 `CHECK` 清单：`design.md` §2.1 / §2.2；
* `attempt_count` / `next_attempt_at` 为 NOT NULL 且默认 `0` / `now()`；`created_at` 为
  **NOT NULL 但不加** DDL 默认值；其余「事件时刻」列不加 DDL 默认值且允许 NULL：
  `design.md` §2.4 / §2.5；
* `messages.parent_id` 自引用外键 + 级联删除：`design.md` §2.2（`NFR-8`）。

**本文件刻意不断言 `CHECK` 的 `sqltext` 正文**（把实现抄进断言 = 同义反复期望值）。这里断言
的是 `CHECK` 的**条数、命名与涉及列**；取值集合与 `constants.py` / `docs/api-contract.yaml`
的三方一致由阶段 5 的 `tests/unit/test_constants_consistency.py` 负责（`plan.md` §7.1）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import CheckConstraint, Enum, Index, Text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from vs_email_forwarding.db import models

if TYPE_CHECKING:
    # 只给类型检查看：`_table()` 的返回标注用它，运行时不需要真的 import。
    from sqlalchemy import Table

# ---------------------------------------------------------------------------
# 期望值：design.md §2.1 的字段清单，拆成「必须显式给值（NOT NULL）」与「允许 NULL」两组。
# 值为 True 表示该列允许 NULL。
# ---------------------------------------------------------------------------

EXPECTED_COLUMNS: dict[str, dict[str, bool]] = {
    "pending_messages": {
        "id": False,
        "direction": False,
        "message_id": True,
        "recipient": False,
        "subject": False,
        "body": False,
        "body_format": False,
        "parent_id": True,
        "mailbox": True,
        "uidvalidity": True,
        "uid": True,
        "attempt_count": False,
        "next_attempt_at": False,
        "last_error_class": True,
        "created_at": False,
    },
    "messages": {
        "id": False,
        "direction": False,
        "status": False,
        "message_id": True,
        "parent_id": True,
        "recipient": False,
        "subject": False,
        "body": False,
        "body_format": False,
        "mailbox": True,
        "uidvalidity": True,
        "uid": True,
        "attempt_count": False,
        "next_attempt_at": False,
        "last_error_class": True,
        "last_smtp_code": True,
        "created_at": False,
        "sent_at": True,
        "received_at": True,
        "reviewed_at": True,
    },
    "rejected_messages": {
        "id": False,
        "direction": False,
        "source": False,
        "subject": True,
        "body": True,
        "last_error_class": True,
        "created_at": False,
        "reviewed_at": True,
    },
    "imap_cursors": {
        "mailbox": False,
        "uidvalidity": False,
        "last_uid": False,
        "updated_at": True,
    },
}

# design.md §2.2：受控枚举列（文本 + CHECK，不用原生 ENUM）。
EXPECTED_ENUM_COLUMNS: dict[str, tuple[str, ...]] = {
    "pending_messages": ("direction", "body_format", "last_error_class"),
    "messages": ("direction", "status", "body_format", "last_error_class"),
    "rejected_messages": ("direction", "source", "last_error_class"),
    "imap_cursors": (),
}

# design.md §2.2：索引 -> (列（顺序即定义顺序）, 是否唯一, 是否为部分索引)。
# 「六项索引」按该节的表格行计：message_id 全局唯一、两处入站幂等部分唯一、messages 认领、
# pending_messages 认领（部分）、回复查询、两表的 created_at 清理。
EXPECTED_INDEXES: dict[str, dict[str, tuple[tuple[str, ...], bool, bool]]] = {
    "pending_messages": {
        "uq_pending_messages_mailbox_uidvalidity_uid": (
            ("mailbox", "uidvalidity", "uid"),
            True,
            True,
        ),
        "ix_pending_messages_direction_next_attempt_at": (
            ("direction", "next_attempt_at"),
            False,
            True,
        ),
    },
    "messages": {
        "uq_messages_message_id": (("message_id",), True, False),
        "uq_messages_mailbox_uidvalidity_uid": (("mailbox", "uidvalidity", "uid"), True, True),
        "ix_messages_status_next_attempt_at": (("status", "next_attempt_at"), False, False),
        "ix_messages_parent_id_received_at": (("parent_id", "received_at"), False, False),
        "ix_messages_created_at": (("created_at",), False, False),
    },
    "rejected_messages": {
        "ix_rejected_messages_created_at": (("created_at",), False, False),
    },
    "imap_cursors": {},
}

# design.md §2.2：CHECK 清单 -> 该约束涉及的列（按语义取自该节的条目，不读 sqltext）。
EXPECTED_CHECKS: dict[str, dict[str, frozenset[str]]] = {
    "pending_messages": {
        "ck_pending_messages_direction_values": frozenset({"direction"}),
        "ck_pending_messages_body_format_values": frozenset({"body_format"}),
        "ck_pending_messages_last_error_class_values": frozenset({"last_error_class"}),
    },
    "messages": {
        "ck_messages_direction_values": frozenset({"direction"}),
        "ck_messages_status_values": frozenset({"status"}),
        "ck_messages_body_format_values": frozenset({"body_format"}),
        "ck_messages_last_error_class_values": frozenset({"last_error_class"}),
        # 入站行只能 received，出站行不得为 received（design.md §2.2）。
        "ck_messages_direction_status": frozenset({"direction", "status"}),
        # 出站行必须有 message_id；入站行必须有 mailbox / uidvalidity / uid。
        "ck_messages_outbound_required": frozenset({"direction", "message_id"}),
        "ck_messages_inbound_required": frozenset({"direction", "mailbox", "uidvalidity", "uid"}),
        # 反向禁止：出站行不得有 mailbox / uidvalidity / uid / parent_id，入站行不得有 message_id。
        "ck_messages_direction_columns": frozenset(
            {"direction", "mailbox", "uidvalidity", "uid", "parent_id", "message_id"}
        ),
    },
    "rejected_messages": {
        "ck_rejected_messages_direction_values": frozenset({"direction"}),
        "ck_rejected_messages_source_values": frozenset({"source"}),
        "ck_rejected_messages_last_error_class_values": frozenset({"last_error_class"}),
        # subject / body 仅入站行非空、出站行必须为 NULL（design.md §2.2）。
        "ck_rejected_messages_inbound_content": frozenset({"direction", "subject", "body"}),
    },
    "imap_cursors": {},
}

# design.md §2.1 / §2.5：`created_at` 是保留期（NFR-8）判据，必须 NOT NULL，
# 且不加 DDL 默认值——漏写要插入失败，而不是静默换掉原始提交时刻。
CREATED_AT_PINNED: tuple[tuple[str, str], ...] = (
    ("pending_messages", "created_at"),
    ("messages", "created_at"),
    ("rejected_messages", "created_at"),
)

# design.md §2.5：其余时间列表示「某个可能尚未发生的事件发生的时刻」，
# 事件没发生就没有值：不加 DDL 默认值、允许 NULL、由应用显式写。
NULLABLE_EVENT_TIMESTAMPS: tuple[tuple[str, str], ...] = (
    ("messages", "sent_at"),
    ("messages", "received_at"),
    ("messages", "reviewed_at"),
    ("rejected_messages", "reviewed_at"),
    ("imap_cursors", "updated_at"),
)


def _table(name: str) -> Table:
    return models.metadata.tables[name]


def _checks(table_name: str) -> list[CheckConstraint]:
    return [c for c in _table(table_name).constraints if isinstance(c, CheckConstraint)]


def _index(table_name: str, index_name: str) -> Index:
    for index in _table(table_name).indexes:
        if index.name == index_name:
            return index
    raise AssertionError(f"{table_name} 缺少索引 {index_name}")


def _column_ddl(table_name: str, column_name: str) -> str:
    """单列在建表 DDL 里的那一行（列类型 + 默认值 + NOT NULL 归属）。"""
    ddl = str(CreateTable(_table(table_name)).compile(dialect=postgresql.dialect()))
    for line in ddl.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{column_name} "):
            return stripped.rstrip(",")
    raise AssertionError(f"{table_name}.{column_name} 未出现在建表 DDL 里")


def _index_ddl(table_name: str, index_name: str) -> str:
    return str(CreateIndex(_index(table_name, index_name)).compile(dialect=postgresql.dialect()))


def test_four_tables_are_declared() -> None:
    # design.md §2：全库物理表 4 张（三张邮件数据表 + 一张游标表）。
    assert sorted(models.metadata.tables) == [
        "imap_cursors",
        "messages",
        "pending_messages",
        "rejected_messages",
    ]


@pytest.mark.parametrize("table_name", sorted(EXPECTED_COLUMNS))
def test_column_names_match_design(table_name: str) -> None:
    assert set(_table(table_name).columns.keys()) == set(EXPECTED_COLUMNS[table_name])


@pytest.mark.parametrize("table_name", sorted(EXPECTED_COLUMNS))
def test_nullability_belongs_to_expected_columns(table_name: str) -> None:
    actual = {column.name: column.nullable for column in _table(table_name).columns}
    assert actual == EXPECTED_COLUMNS[table_name]


@pytest.mark.parametrize("table_name", sorted(EXPECTED_ENUM_COLUMNS))
def test_controlled_columns_are_text_backed_by_check(table_name: str) -> None:
    # 受控枚举一律以文本列存储，取值合法性由 CHECK 兜底（design.md §2.1 / §2.2）。
    check_names = {check.name for check in _checks(table_name)}
    for column_name in EXPECTED_ENUM_COLUMNS[table_name]:
        assert isinstance(_table(table_name).c[column_name].type, Text)
        assert f"ck_{table_name}_{column_name}_values" in check_names


def test_no_native_enum_type_is_used() -> None:
    # design.md §2.1：不使用 PostgreSQL 原生 ENUM。
    for table in models.metadata.tables.values():
        for column in table.columns:
            assert not isinstance(column.type, Enum), f"{table.name}.{column.name} 用了原生枚举类型"


@pytest.mark.parametrize("table_name", sorted(EXPECTED_INDEXES))
def test_index_names_match_design(table_name: str) -> None:
    assert {index.name for index in _table(table_name).indexes} == set(EXPECTED_INDEXES[table_name])


@pytest.mark.parametrize(
    ("table_name", "index_name", "columns", "unique"),
    [
        (table, index, spec[0], spec[1])
        for table, indexes in sorted(EXPECTED_INDEXES.items())
        for index, spec in sorted(indexes.items())
    ],
)
def test_index_columns_and_uniqueness(
    table_name: str, index_name: str, columns: tuple[str, ...], unique: bool
) -> None:
    index = _index(table_name, index_name)
    assert tuple(column.name for column in index.columns) == columns
    assert index.unique is unique


@pytest.mark.parametrize("table_name", ["messages", "pending_messages"])
def test_inbound_dedup_index_is_partial_on_inbound_direction(table_name: str) -> None:
    # design.md §2.2：两处入站幂等索引都是部分唯一索引 WHERE direction = 'inbound'。
    ddl = _index_ddl(table_name, f"uq_{table_name}_mailbox_uidvalidity_uid")
    assert "WHERE direction = 'inbound'" in ddl
    assert ddl.startswith("CREATE UNIQUE INDEX")


def test_pending_claim_index_is_partial_on_claimable_attempt_count() -> None:
    # design.md §2.2：认领索引的部分谓词是 attempt_count <= 4（收割支仍能命中）。
    ddl = _index_ddl("pending_messages", "ix_pending_messages_direction_next_attempt_at")
    assert "WHERE attempt_count <= 4" in ddl


@pytest.mark.parametrize(
    ("table_name", "index_name"),
    [
        ("messages", "uq_messages_message_id"),
        ("messages", "ix_messages_status_next_attempt_at"),
        ("messages", "ix_messages_parent_id_received_at"),
        ("messages", "ix_messages_created_at"),
        ("rejected_messages", "ix_rejected_messages_created_at"),
    ],
)
def test_non_partial_indexes_have_no_predicate(table_name: str, index_name: str) -> None:
    assert " WHERE " not in _index_ddl(table_name, index_name)


def test_parent_id_is_self_referential_foreign_key_with_cascade() -> None:
    # design.md §2.2 + NFR-8：邮件与它的回复一起移除，靠自引用外键的级联删除。
    messages = models.metadata.tables["messages"]
    foreign_keys = list(messages.c.parent_id.foreign_keys)
    assert len(foreign_keys) == 1, "parent_id 只应有一条外键"
    foreign_key = foreign_keys[0]
    assert foreign_key.target_fullname == "messages.id"
    assert foreign_key.column.table is messages
    assert foreign_key.ondelete == "CASCADE"


@pytest.mark.parametrize("table_name", sorted(EXPECTED_CHECKS))
def test_check_constraint_names_and_count_match_design(table_name: str) -> None:
    assert {check.name for check in _checks(table_name)} == set(EXPECTED_CHECKS[table_name])


@pytest.mark.parametrize("table_name", sorted(EXPECTED_CHECKS))
def test_check_constraints_cover_expected_columns(table_name: str) -> None:
    actual = {
        check.name: frozenset(column.name for column in check.columns)
        for check in _checks(table_name)
    }
    assert actual == EXPECTED_CHECKS[table_name]


@pytest.mark.parametrize(("table_name", "column_name"), CREATED_AT_PINNED)
def test_created_at_is_not_null_without_ddl_default(table_name: str, column_name: str) -> None:
    # design.md §2.1 / §2.5：保留期判据非空；DDL 里恰好是「NOT NULL 且没有 DEFAULT」。
    column = _table(table_name).c[column_name]
    assert column.nullable is False
    assert column.server_default is None
    assert (
        _column_ddl(table_name, column_name) == f"{column_name} TIMESTAMP WITH TIME ZONE NOT NULL"
    )


@pytest.mark.parametrize(("table_name", "column_name"), NULLABLE_EVENT_TIMESTAMPS)
def test_nullable_event_timestamp_has_no_ddl_default(table_name: str, column_name: str) -> None:
    # 事件时刻列不加 DDL 默认值、允许 NULL，取时点只有应用一处（design.md §2.5）。
    column = _table(table_name).c[column_name]
    assert column.nullable is True
    assert column.server_default is None


@pytest.mark.parametrize("table_name", ["messages", "pending_messages"])
def test_attempt_columns_are_not_null_with_pinned_defaults(table_name: str) -> None:
    # design.md §2.4：建行即 attempt_count = 0、next_attempt_at = now()。
    assert _column_ddl(table_name, "attempt_count") == "attempt_count INTEGER DEFAULT 0 NOT NULL"
    assert (
        _column_ddl(table_name, "next_attempt_at")
        == "next_attempt_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL"
    )


@pytest.mark.parametrize("table_name", ["messages", "pending_messages"])
def test_body_format_defaults_to_text(table_name: str) -> None:
    # design.md §2.1：body_format「文本 + CHECK：text / html，默认 text」。
    assert _column_ddl(table_name, "body_format") == "body_format TEXT DEFAULT 'text' NOT NULL"
