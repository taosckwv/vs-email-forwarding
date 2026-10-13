"""首版迁移：四张邮件表、六项索引（八条 ``Index``）、全部 ``CHECK`` 与一处自引用外键。

真源：`specs/0001-email-forwarding-service/design.md` §2.1 / §2.2；落点、顺序与
`downgrade()` 要求见同特性 `plan.md` §3.1 / §3.2；文件名与实现约定见 `migrations/README.md`。

## 为什么手写，而不是 ``--autogenerate``

``--autogenerate`` 看不见两类 DDL 事实：**部分索引的 ``WHERE`` 谓词**（``WHERE direction =
'inbound'``、``WHERE attempt_count <= 4``）与**跨列 ``CHECK``**（``ck_messages_direction_status``
等）。它只按列的类型与可空性、索引的名字与列来比对，谓词一丢就等于把「入站幂等」与
「认领面」悄悄放走——恰恰是 `design.md` §2.2 里最要紧的几条。所以本文件按 §2.2 手写，
并与 ``db/models.py`` 的 ``metadata`` 逐表逐列对齐。

## 取值字面量为什么不 import ``constants.py``

受控枚举以文本列 + ``CHECK`` 存储，取值文本在本文件与 ``db/models.py`` 各持一份，
**DDL 自洽**、互不 import（`plan.md` §2.9）；三方一致性由阶段 5 的
``tests/unit/test_constants_consistency.py`` 断言。``CHECK`` 的 SQL 直接写字面量、
不做字符串拼装——这种地方可读性就是可审性。

## 六项索引与八条 ``Index``

`design.md` §2.2 列六项索引，其中「``(mailbox, uidvalidity, uid)`` 部分唯一」落在
``pending_messages`` 与 ``messages`` 各一份、「``(created_at)``」也落在两张表上，
因此本文件建 **8 条** ``Index``，名字与谓词逐条对齐 ``db/models.py``。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

# 入站幂等索引的谓词：只对入站行唯一（design.md §2.2）。两处索引共用。
_INBOUND_ONLY = "direction = 'inbound'"
# 待审表的认领索引谓词上界（design.md §2.4 / §3 阶段①）。
_CLAIMABLE = "attempt_count <= 4"


def upgrade() -> None:
    """一次建齐：四张表 + 8 条索引 + 15 条 `CHECK` + 一处自引用外键。"""
    op.create_table(
        "pending_messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=True),
        sa.Column("recipient", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("body_format", sa.Text(), nullable=False, server_default=sa.text("'text'")),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("mailbox", sa.Text(), nullable=True),
        sa.Column("uidvalidity", sa.Integer(), nullable=True),
        sa.Column("uid", sa.Integer(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_error_class", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "direction IN ('outbound', 'inbound')",
            name="ck_pending_messages_direction_values",
        ),
        sa.CheckConstraint(
            "body_format IN ('text', 'html')",
            name="ck_pending_messages_body_format_values",
        ),
        sa.CheckConstraint(
            "last_error_class IS NULL"
            " OR last_error_class IN ('permanent', 'transient', 'internal')",
            name="ck_pending_messages_last_error_class_values",
        ),
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=True),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("recipient", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("body_format", sa.Text(), nullable=False, server_default=sa.text("'text'")),
        sa.Column("mailbox", sa.Text(), nullable=True),
        sa.Column("uidvalidity", sa.Integer(), nullable=True),
        sa.Column("uid", sa.Integer(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_error_class", sa.Text(), nullable=True),
        sa.Column("last_smtp_code", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "direction IN ('outbound', 'inbound')",
            name="ck_messages_direction_values",
        ),
        sa.CheckConstraint(
            "status IN ('sending', 'sent', 'failed', 'received')",
            name="ck_messages_status_values",
        ),
        sa.CheckConstraint(
            "body_format IN ('text', 'html')",
            name="ck_messages_body_format_values",
        ),
        sa.CheckConstraint(
            "last_error_class IS NULL"
            " OR last_error_class IN ('permanent', 'transient', 'internal')",
            name="ck_messages_last_error_class_values",
        ),
        # 入站行只能是 received，出站行不得为 received（design.md §2.2）。
        sa.CheckConstraint(
            "(direction = 'outbound' AND status IN ('sending', 'sent', 'failed'))"
            " OR (direction = 'inbound' AND status = 'received')",
            name="ck_messages_direction_status",
        ),
        # 出站行必须有 message_id（入站回复关联的解析依据）。
        sa.CheckConstraint(
            "direction = 'inbound' OR message_id IS NOT NULL",
            name="ck_messages_outbound_required",
        ),
        # 入站行必须有 mailbox / uidvalidity / uid（入站幂等键）。
        sa.CheckConstraint(
            "direction = 'outbound'"
            " OR (mailbox IS NOT NULL AND uidvalidity IS NOT NULL AND uid IS NOT NULL)",
            name="ck_messages_inbound_required",
        ),
        # 反向禁止：出站行不得有入站三列与 parent_id，入站行不得有 message_id——
        # 堵住混装行的 NULL 语义漏洞（design.md §2.2）。
        sa.CheckConstraint(
            "(direction = 'outbound' AND mailbox IS NULL AND uidvalidity IS NULL"
            " AND uid IS NULL AND parent_id IS NULL)"
            " OR (direction = 'inbound' AND message_id IS NULL)",
            name="ck_messages_direction_columns",
        ),
        # 自引用外键 + 级联删除：父行被清理时回复一起移除（NFR-8）。
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["messages.id"],
            name="fk_messages_parent_id",
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "rejected_messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("last_error_class", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "direction IN ('outbound', 'inbound')",
            name="ck_rejected_messages_direction_values",
        ),
        sa.CheckConstraint(
            "source IN ('review_rejected', 'review_error', 'review_incomplete')",
            name="ck_rejected_messages_source_values",
        ),
        sa.CheckConstraint(
            "last_error_class IS NULL"
            " OR last_error_class IN ('permanent', 'transient', 'internal')",
            name="ck_rejected_messages_last_error_class_values",
        ),
        # subject / body 仅入站行非空、出站行必须为 NULL（design.md §2.2）。
        sa.CheckConstraint(
            "(direction = 'outbound' AND subject IS NULL AND body IS NULL)"
            " OR (direction = 'inbound' AND subject IS NOT NULL AND body IS NOT NULL)",
            name="ck_rejected_messages_inbound_content",
        ),
    )

    op.create_table(
        "imap_cursors",
        sa.Column("mailbox", sa.Text(), primary_key=True),
        sa.Column("uidvalidity", sa.Integer(), nullable=False),
        sa.Column("last_uid", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # --- 六项索引 / 8 条 Index：谓词必须显式给出，autogenerate 认不出它们 -------------------
    # 入站幂等（AC-11）：两处都建，游标回退时在落待审行那一步就被挡住。
    op.create_index(
        "uq_pending_messages_mailbox_uidvalidity_uid",
        "pending_messages",
        ["mailbox", "uidvalidity", "uid"],
        unique=True,
        postgresql_where=sa.text(_INBOUND_ONLY),
    )
    # 流① / 流④ 的认领索引：首版即建，谓词含收割支的命中面（design.md §2.2）。
    op.create_index(
        "ix_pending_messages_direction_next_attempt_at",
        "pending_messages",
        ["direction", "next_attempt_at"],
        postgresql_where=sa.text(_CLAIMABLE),
    )
    # 出站行必填，也是入站回复关联的解析依据。
    op.create_index("uq_messages_message_id", "messages", ["message_id"], unique=True)
    op.create_index(
        "uq_messages_mailbox_uidvalidity_uid",
        "messages",
        ["mailbox", "uidvalidity", "uid"],
        unique=True,
        postgresql_where=sa.text(_INBOUND_ONLY),
    )
    # 流② 的 FOR UPDATE SKIP LOCKED 抢占（plan.md §5.1）。
    op.create_index("ix_messages_status_next_attempt_at", "messages", ["status", "next_attempt_at"])
    # GET /v1/messages/{id}/replies 的 keyset 翻页（AC-41）。
    op.create_index("ix_messages_parent_id_received_at", "messages", ["parent_id", "received_at"])
    # 保留期清理的判据（design.md §3.6）。
    op.create_index("ix_messages_created_at", "messages", ["created_at"])
    op.create_index("ix_rejected_messages_created_at", "rejected_messages", ["created_at"])


def downgrade() -> None:
    """按依赖倒序删四张表：`messages`（自引用外键所在表）→ 其余三张。

    索引、`CHECK`、主键与外键都由各自的表持有，`DROP TABLE` 会把它们一起干净移除，
    因此这里不重复 `drop_index` / `drop_constraint`（首版没有丢数据的不可逆步骤）。
    """
    op.drop_table("messages")
    op.drop_table("pending_messages")
    op.drop_table("rejected_messages")
    op.drop_table("imap_cursors")
