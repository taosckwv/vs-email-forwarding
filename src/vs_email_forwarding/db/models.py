#!/usr/bin/env python3
"""四张表的 ORM 模型（数据模型真源：`design.md` §2）。

## 表与落点

* ``pending_messages``（待审表）：在审邮件的唯一落点，出站与入站共用、``direction`` 区分；
  **不设 ``status`` 列**——表里只剩「等待审查」一种行。
* ``messages``（邮件表）：出站邮件与已入库的入站回复共表；``parent_id`` 自引用外键 +
  ``ON DELETE CASCADE``，让「邮件与它的回复一起移除」由数据库保证（``NFR-8``）。
* ``rejected_messages``（拒审表）：审查判定不通过或审查环节出错的行。出站拒审不保留原文；
  入站拒审保留主题与正文供复核，由 ``CHECK`` 保证只有入站行非空。
* ``imap_cursors``（游标表）：IMAP 拉取进度，每个信箱文件夹一行。表里没有别的标识列，
  因此 ``mailbox`` 直接作主键（``design.md`` §2.2 的「``mailbox`` 唯一」）。

## 取值文本为什么在本模块再写一份

受控枚举以**文本列 + ``CHECK``** 兜底，不用 PostgreSQL 原生 ``ENUM``（``design.md``
§2.1 / §2.2）。取值集合的真源是 ``design.md`` §2.1 的受控枚举取值表，运行期取值的唯一
出处是 ``constants.py``；**DDL 必须自洽**，所以本模块与首版迁移各自持一份字面量，
**不 import ``constants.py``**（约定见 ``plan.md`` §2.9；三方一致性由阶段 5 的
``tests/unit/test_constants_consistency.py`` 断言）。

## 列的可空性口径

``attempt_count`` / ``next_attempt_at`` 为 NOT NULL 且带 DDL 默认值 ``0`` / ``now()``：
建行即等于 ``0`` 与当前时刻，抢占查询可直接用（``design.md`` §2.4）。``created_at`` 为
**NOT NULL 但不加** DDL 默认值：它是保留期（``NFR-8``）判据、不可为空，而漏写这一列必须是
**插入失败**而不是静默换成搬运时刻（``design.md`` §2.5）。**其余「事件时刻」列不加 DDL
默认值、允许 NULL**，由应用在写入时显式给值——取时点只有应用一处，不用 ``func.now()``
兜底（``design.md`` §2.5、``plan.md`` §5.2）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# ---------------------------------------------------------------------------
# 取值字面量（DDL 自洽，本模块不 import constants.py）
#
# 真源：design.md §2.1 的受控枚举取值表。
# ---------------------------------------------------------------------------

_OUTBOUND: Final = "outbound"
_INBOUND: Final = "inbound"
_DIRECTIONS: Final[tuple[str, ...]] = (_OUTBOUND, _INBOUND)

_OUTBOUND_STATUS_VALUES: Final[tuple[str, ...]] = ("sending", "sent", "failed")
_RECEIVED_STATUS: Final = "received"
_MESSAGES_STATUS_VALUES: Final[tuple[str, ...]] = (*_OUTBOUND_STATUS_VALUES, _RECEIVED_STATUS)

_BODY_FORMAT_VALUES: Final[tuple[str, ...]] = ("text", "html")
_DEFAULT_BODY_FORMAT: Final = "text"

_ERROR_CLASS_VALUES: Final[tuple[str, ...]] = ("permanent", "transient", "internal")

_REJECT_SOURCE_VALUES: Final[tuple[str, ...]] = (
    "review_rejected",
    "review_error",
    "review_incomplete",
)

#: 认领谓词上界：``attempt_count <= 4``——放宽到 4 是为了让「第 4 次尝试已认领、结果事务
#: 未提交」的遗留行仍能被认领、进而由收割支推进终态（``design.md`` §2.4 / §3 阶段①）。
_MAX_CLAIMABLE_ATTEMPT_COUNT: Final = 4


class Base(DeclarativeBase):
    """四张表的声明式基类。"""


#: 元数据入口：迁移的 ``target_metadata``（``migrations/env.py``）与元数据单测都读它。
metadata = Base.metadata


def _enum_check(column_name: str, values: tuple[str, ...], name: str) -> sa.CheckConstraint:
    """受控枚举列的取值约束（文本列 + ``CHECK``，不用原生 ``ENUM``）。"""
    return sa.CheckConstraint(sa.column(column_name).in_(values), name=name)


def _nullable_enum_check(
    column_name: str, values: tuple[str, ...], name: str
) -> sa.CheckConstraint:
    """可为空的受控枚举列：NULL 合法，非 NULL 时必须在取值集合内。"""
    return sa.CheckConstraint(
        sa.or_(sa.column(column_name).is_(None), sa.column(column_name).in_(values)), name=name
    )


class PendingMessage(Base):
    """待审表：在审邮件的唯一落点（`design.md` §2.1）。

    过审即删行，因此它只承载在审队列、不随邮件量增长；状态由「这一行在哪张表里」承担，
    所以没有 ``status`` 列。``FR-11`` 的「审查中」由「待审行且 ``attempt_count > 0``」兑现。
    """

    __tablename__ = "pending_messages"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    direction: Mapped[str] = mapped_column(sa.Text, nullable=False)
    message_id: Mapped[str | None] = mapped_column(sa.Text)
    recipient: Mapped[str] = mapped_column(sa.Text, nullable=False)
    subject: Mapped[str] = mapped_column(sa.Text, nullable=False)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    body_format: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=sa.literal(_DEFAULT_BODY_FORMAT)
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    mailbox: Mapped[str | None] = mapped_column(sa.Text)
    uidvalidity: Mapped[int | None] = mapped_column(sa.Integer)
    uid: Mapped[int | None] = mapped_column(sa.Integer)
    attempt_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.literal(0)
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    last_error_class: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    __table_args__ = (
        _enum_check("direction", _DIRECTIONS, "ck_pending_messages_direction_values"),
        _enum_check("body_format", _BODY_FORMAT_VALUES, "ck_pending_messages_body_format_values"),
        _nullable_enum_check(
            "last_error_class", _ERROR_CLASS_VALUES, "ck_pending_messages_last_error_class_values"
        ),
        # 入站幂等：同一 UIDVALIDITY 内 (mailbox, uidvalidity, uid) 唯一（design.md §2.2）。
        sa.Index(
            "uq_pending_messages_mailbox_uidvalidity_uid",
            "mailbox",
            "uidvalidity",
            "uid",
            unique=True,
            postgresql_where=sa.column("direction") == _INBOUND,
        ),
        # 流① / 流④ 的认领索引，首版即建，谓词含收割支的命中面（design.md §2.2）。
        sa.Index(
            "ix_pending_messages_direction_next_attempt_at",
            "direction",
            "next_attempt_at",
            postgresql_where=sa.column("attempt_count") <= _MAX_CLAIMABLE_ATTEMPT_COUNT,
        ),
    )


class Message(Base):
    """邮件表：审查通过后的出站邮件与已入库的入站回复（`design.md` §2.1）。

    单表承载两类行，靠 ``direction`` 区分；行模型的必填与反向禁止、以及
    ``direction`` 与 ``status`` 的合法组合都由 ``CHECK`` 钉在库里（`design.md` §2.2）。
    """

    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    direction: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    message_id: Mapped[str | None] = mapped_column(sa.Text)
    # 自引用外键 + 级联删除：父行被清理时回复一起移除（NFR-8）。
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("messages.id", ondelete="CASCADE", name="fk_messages_parent_id"),
    )
    recipient: Mapped[str] = mapped_column(sa.Text, nullable=False)
    subject: Mapped[str] = mapped_column(sa.Text, nullable=False)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    body_format: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=sa.literal(_DEFAULT_BODY_FORMAT)
    )
    mailbox: Mapped[str | None] = mapped_column(sa.Text)
    uidvalidity: Mapped[int | None] = mapped_column(sa.Integer)
    uid: Mapped[int | None] = mapped_column(sa.Integer)
    attempt_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.literal(0)
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    last_error_class: Mapped[str | None] = mapped_column(sa.Text)
    last_smtp_code: Mapped[int | None] = mapped_column(sa.Integer)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        _enum_check("direction", _DIRECTIONS, "ck_messages_direction_values"),
        _enum_check("status", _MESSAGES_STATUS_VALUES, "ck_messages_status_values"),
        _enum_check("body_format", _BODY_FORMAT_VALUES, "ck_messages_body_format_values"),
        _nullable_enum_check(
            "last_error_class", _ERROR_CLASS_VALUES, "ck_messages_last_error_class_values"
        ),
        # 入站行只能是 received，出站行不得为 received（design.md §2.2）。
        sa.CheckConstraint(
            sa.or_(
                sa.and_(
                    sa.column("direction") == _OUTBOUND,
                    sa.column("status").in_(_OUTBOUND_STATUS_VALUES),
                ),
                sa.and_(
                    sa.column("direction") == _INBOUND,
                    sa.column("status") == _RECEIVED_STATUS,
                ),
            ),
            name="ck_messages_direction_status",
        ),
        # 出站行必须有 message_id（它是入站回复关联的唯一依据）。
        sa.CheckConstraint(
            sa.or_(
                sa.column("direction") == _INBOUND,
                sa.column("message_id").is_not(None),
            ),
            name="ck_messages_outbound_required",
        ),
        # 入站行必须有 mailbox / uidvalidity / uid（入站幂等键）。
        sa.CheckConstraint(
            sa.or_(
                sa.column("direction") == _OUTBOUND,
                sa.and_(
                    sa.column("mailbox").is_not(None),
                    sa.column("uidvalidity").is_not(None),
                    sa.column("uid").is_not(None),
                ),
            ),
            name="ck_messages_inbound_required",
        ),
        # 反向禁止：出站行不得有 mailbox / uidvalidity / uid / parent_id，入站行不得有
        # message_id——堵住混装行的 NULL 语义漏洞（design.md §2.2）。
        sa.CheckConstraint(
            sa.or_(
                sa.and_(
                    sa.column("direction") == _OUTBOUND,
                    sa.column("mailbox").is_(None),
                    sa.column("uidvalidity").is_(None),
                    sa.column("uid").is_(None),
                    sa.column("parent_id").is_(None),
                ),
                sa.and_(
                    sa.column("direction") == _INBOUND,
                    sa.column("message_id").is_(None),
                ),
            ),
            name="ck_messages_direction_columns",
        ),
        sa.Index("uq_messages_message_id", "message_id", unique=True),
        sa.Index(
            "uq_messages_mailbox_uidvalidity_uid",
            "mailbox",
            "uidvalidity",
            "uid",
            unique=True,
            postgresql_where=sa.column("direction") == _INBOUND,
        ),
        sa.Index("ix_messages_status_next_attempt_at", "status", "next_attempt_at"),
        sa.Index("ix_messages_parent_id_received_at", "parent_id", "received_at"),
        sa.Index("ix_messages_created_at", "created_at"),
    )


class RejectedMessage(Base):
    """拒审表：审查判定不通过或审查环节出错的行（`design.md` §2.1）。

    出站拒审不保留原文（收件人、主题、正文、格式都不存），入站拒审保留主题与正文供复核；
    ``CHECK`` 保证只有入站行非空。
    """

    __tablename__ = "rejected_messages"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    direction: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source: Mapped[str] = mapped_column(sa.Text, nullable=False)
    subject: Mapped[str | None] = mapped_column(sa.Text)
    body: Mapped[str | None] = mapped_column(sa.Text)
    last_error_class: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        _enum_check("direction", _DIRECTIONS, "ck_rejected_messages_direction_values"),
        _enum_check("source", _REJECT_SOURCE_VALUES, "ck_rejected_messages_source_values"),
        _nullable_enum_check(
            "last_error_class",
            _ERROR_CLASS_VALUES,
            "ck_rejected_messages_last_error_class_values",
        ),
        # subject / body 仅入站行非空、出站行必须为 NULL（design.md §2.2）。
        sa.CheckConstraint(
            sa.or_(
                sa.and_(
                    sa.column("direction") == _OUTBOUND,
                    sa.column("subject").is_(None),
                    sa.column("body").is_(None),
                ),
                sa.and_(
                    sa.column("direction") == _INBOUND,
                    sa.column("subject").is_not(None),
                    sa.column("body").is_not(None),
                ),
            ),
            name="ck_rejected_messages_inbound_content",
        ),
        sa.Index("ix_rejected_messages_created_at", "created_at"),
    )


class ImapCursor(Base):
    """游标表：IMAP 拉取进度，每个信箱文件夹一行（`design.md` §2.1）。

    ``UIDVALIDITY`` 一变就重置 ``last_uid``：这条规则在表结构上一眼可见。表里没有别的
    标识列，因此 ``mailbox`` 直接作主键，同时兑现 §2.2 的「``mailbox`` 唯一」。
    """

    __tablename__ = "imap_cursors"

    mailbox: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    uidvalidity: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    last_uid: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


__all__ = [
    "Base",
    "ImapCursor",
    "Message",
    "PendingMessage",
    "RejectedMessage",
    "metadata",
]
