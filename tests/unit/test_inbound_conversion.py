#!/usr/bin/env python3
"""`_to_inbound_email` 的结构字段边界转换 + `FetchInboundResult` 的两条非零分支（`T-36` 并入的 `T-46` 收口遗留）。

**边界在哪**：`providers/imap/` 交出中性结构（`FetchedMessage` / `FetchedPart`），`core/inbound.py` 的判定
吃另一份契约（`InboundEmail` / `MimePart`）——两侧各自声明、`providers/` 不得 import `core/`
（`docs/architecture.md` §2），转换点在 `flows/fetch_inbound.py::_to_inbound_email`。这条转换是**判定输入的
唯一来源**：任何一项落空，退信判定、原邮件标识、`4.x` / `5.x` 分类都会静默走偏，而落库侧只会看到一个
「丢弃原因不同」的结果。本文件按结构字段逐项钉住它（7 条），并补上两条计数分支（2 条）。

**期望值来源**（`docs/testing.md` R2）：结构字段的期望值取自 `providers/imap/base.py` 的字段契约（中性结构
交的是什么），判定结论的期望值取自 `design.md` §3.4——退信 = `multipart/report` 容器 **且** 含
`message/delivery-status` 部分；阅读回执 = 含 `message/disposition-notification` 部分；原邮件标识取内嵌
`message/rfc822` 的 `Message-ID`；`Status` 的 `5.x.x` → permanent、`4.x.x` → transient。

**不碰数据库与网络**（`AGENTS.md` §4）：只把「IMAP 边界」与「仓储边界」换成捕获型替身
（`docs/testing.md` §3.5），判定函数 `core/inbound.py` 本身仍是真实现——本层不伪造判定结果。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import fields, replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy.exc import SQLAlchemyError

from vs_email_forwarding.config import Settings
from vs_email_forwarding.constants import ERROR_CLASS_PERMANENT
from vs_email_forwarding.core.inbound import (
    InboundEmail,
    MimePart,
    is_delivery_status_notification,
    is_mdn,
    parse_dsn,
    scan_references,
)
from vs_email_forwarding.flows import fetch_inbound
from vs_email_forwarding.flows.fetch_inbound import FetchInboundResult, _to_inbound_email
from vs_email_forwarding.providers.imap.base import (
    FetchedMessage,
    FetchedPart,
    FetchResult,
    MailboxState,
    MailboxStateResult,
)

#: 本次判定的注入时刻（`plan.md` §7.2：时间推进不靠 sleep）。
_NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)

#: 出站行的发出时刻：**刻意与 `_NOW` 不同**——退信覆写的是已发出的行，这两个时刻必须可分辨。
_SENT_AT = datetime(2025, 5, 31, 9, 0, 0, tzinfo=UTC)

#: 夹具内容一律 `example.com` 占位（`AGENTS.md` §8 第 4 条、`docs/testing.md` §4.7）。
_MAILBOX = "INBOX"
_UIDVALIDITY = 4242
_MESSAGE_ID = "<outbound-1@example.com>"
_FROM = "replier@example.com"

#: 出站行的主键（替身返回值里的稳定标识；真库里的取值由集成用例负责）。
_TARGET_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")

#: `FetchInboundResult` 的字段集（`design.md` §3.4 的计数字段）——多一个字段就说明口径变了。
_RESULT_FIELDS = frozenset({"fetched", "stored", "dsn_failed", "discarded", "errored"})

#: `MimePart` 的字段集（`core/inbound.py` 的判定输入）：**没有** payload 通道——正文由
#: `flows/` 从 `FetchedPart.payload` 另取，不经过判定输入（边界职责划分）。
_MIME_PART_FIELDS = frozenset({"content_type", "dsn_status", "embedded_message_id"})


def _part(
    content_type: str,
    *,
    payload: str | None = None,
    dsn_status: str | None = None,
    embedded_message_id: str | None = None,
) -> FetchedPart:
    """一个中性部件（四个字段逐项给出：默认值是否被显式传过，是契约的一部分）。"""
    return FetchedPart(
        content_type=content_type,
        payload=payload,
        dsn_status=dsn_status,
        embedded_message_id=embedded_message_id,
    )


def _dsn_parts(
    *, status: str = "5.1.1", original: str | None = _MESSAGE_ID, payload: str | None = None
) -> tuple[FetchedPart, ...]:
    """一封真退信的部件清单：`delivery-status`（带 `Status`）+ 内嵌原邮件的 `message/rfc822`。"""
    return (
        _part("message/delivery-status", dsn_status=status),
        _part("message/rfc822", payload=payload, embedded_message_id=original),
    )


def _dsn_message(
    *,
    status: str = "5.1.1",
    original: str | None = _MESSAGE_ID,
    content_type: str = "multipart/report",
    payload: str | None = None,
    uid: int = 1,
) -> FetchedMessage:
    """一封中性结构形态的退信（`design.md` §3.4 的退信形状）。"""
    return _neutral(
        uid=uid,
        content_type=content_type,
        headers={"from": "mailer-daemon@example.com"},
        parts=_dsn_parts(status=status, original=original, payload=payload),
    )


def _mdn_message(*, uid: int = 1) -> FetchedMessage:
    """一封阅读回执：`multipart/report` 容器 + `message/disposition-notification` 部件。

    容器刻意取 `multipart/report`——它与退信同容器，因此「不是退信」只能由**部件**判出来
    （`design.md` §3.4：退信要容器 + `delivery-status` 两条同时成立）。
    """
    return _neutral(
        uid=uid,
        content_type="multipart/report",
        headers={"from": _FROM},
        parts=(_part("message/disposition-notification"),),
    )


def _reply_message(
    *, uid: int = 1, headers: Mapping[str, str] | None = None, body: str = "回复正文"
) -> FetchedMessage:
    """一封能命中出站邮件的普通回复（中性结构）。"""
    if headers is None:
        headers = {"from": _FROM, "in-reply-to": _MESSAGE_ID, "subject": "回复主题"}
    return _neutral(
        uid=uid,
        content_type="text/plain",
        headers=headers,
        parts=(_part("text/plain", payload=body),),
    )


def _neutral(
    *, uid: int, content_type: str, headers: Mapping[str, str], parts: Sequence[FetchedPart]
) -> FetchedMessage:
    """一封中性结构邮件（三要素 + 解析结果；三要素就是入站幂等键，`design.md` §2.2）。"""
    return FetchedMessage(
        mailbox=_MAILBOX,
        uidvalidity=_UIDVALIDITY,
        uid=uid,
        content_type=content_type,
        headers=dict(headers),
        parts=tuple(parts),
    )


def _probe_message(*, payload: str) -> FetchedMessage:
    """结构字段全缺、payload 全是可识别串的探针：用来判「转换看不看 payload」。"""
    return _neutral(
        uid=1,
        content_type="multipart/report",
        headers={"from": _FROM},
        parts=(
            _part("message/delivery-status", payload=f"{payload}-状态块"),
            _part("message/rfc822", payload=f"{payload}-原邮件"),
        ),
    )


class TestStructuralFieldBoundary:
    """7 条结构字段边界：每条同时钉「字段透传」与「依赖它的结构判定」。

    只钉字段透传不够——字段搬对了但判定链断掉，落库侧看到的是另一种丢弃原因；只钉判定结论也不够
    ——判定可能恰好被别的字段满足。两侧一起断言，任一处的转换改动都会在这里显形。
    """

    def test_content_type_is_carried_and_decides_the_dsn_container(self) -> None:
        message = _dsn_message()
        converted = _to_inbound_email(message)

        assert converted.content_type == message.content_type == "multipart/report"
        assert is_delivery_status_notification(converted) is True
        # 只把容器换掉（部件不变）：退信判定必须落空——判据是「容器 + 部分」两条同时成立。
        assert (
            is_delivery_status_notification(replace(converted, content_type="text/plain")) is False
        )

    def test_headers_are_carried_verbatim(self) -> None:
        headers = {
            "from": _FROM,
            "in-reply-to": "<direct-parent@example.com>",
            "references": "<older@example.com>",
            "subject": "边界主题",
        }
        converted = _to_inbound_email(_reply_message(headers=headers))

        assert dict(converted.headers) == headers
        # 引用解析吃的就是这份头（`In-Reply-To` 在前、`References` 在后、去重保序）。
        assert scan_references(converted.headers) == (
            "<direct-parent@example.com>",
            "<older@example.com>",
        )

    def test_parts_keep_their_count_and_order(self) -> None:
        message = _dsn_message(uid=7)
        converted = _to_inbound_email(message)

        assert len(converted.parts) == len(message.parts) == 2
        assert [part.content_type for part in converted.parts] == [
            part.content_type for part in message.parts
        ]
        # 顺序有语义：`parse_dsn` 逐部件取「第一个」`Status` 与内嵌原邮件标识。
        assert parse_dsn(converted).original_message_id == _MESSAGE_ID

    def test_part_content_type_selects_the_delivery_status_and_mdn_parts(self) -> None:
        converted_dsn = _to_inbound_email(_dsn_message())
        assert [part.content_type for part in converted_dsn.parts] == [
            "message/delivery-status",
            "message/rfc822",
        ]

        converted_mdn = _to_inbound_email(_mdn_message())
        assert [part.content_type for part in converted_mdn.parts] == [
            "message/disposition-notification"
        ]
        assert is_mdn(converted_mdn) is True
        assert is_delivery_status_notification(converted_mdn) is False
        # 部件类型全认不出 ⇒ 两条结构判定都落空（回执会被当成普通回复）。
        blanked = tuple(replace(part, content_type="text/plain") for part in converted_mdn.parts)
        assert is_mdn(replace(converted_mdn, parts=blanked)) is False

    def test_part_dsn_status_is_carried_and_classifies(self) -> None:
        converted = _to_inbound_email(_dsn_message(status="5.1.1"))

        assert converted.parts[0].dsn_status == "5.1.1"
        assert parse_dsn(converted).status_class == "permanent"
        assert (
            parse_dsn(_to_inbound_email(_dsn_message(status="4.2.0"))).status_class == "transient"
        )
        # 非 delivery-status 部件原样带过 `None`：转换不发明取值（否则分类会吃到垃圾值）。
        assert converted.parts[1].dsn_status is None

    def test_part_embedded_message_id_is_carried_and_yields_the_original_id(self) -> None:
        converted = _to_inbound_email(_dsn_message(original=_MESSAGE_ID))

        assert converted.parts[1].embedded_message_id == _MESSAGE_ID
        assert parse_dsn(converted).original_message_id == _MESSAGE_ID
        assert converted.parts[0].embedded_message_id is None
        # 取不出内嵌原邮件时是 `None`（调用方按 `dsn_without_id` 丢弃，`design.md` §3.4）。
        assert parse_dsn(_to_inbound_email(_dsn_message(original=None))).original_message_id is None

    def test_conversion_ignores_payloads(self) -> None:
        # 判定输入里没有正文通道：结构相同、payload 不同的两封，转换结果必须逐字段相等，且等于
        # 按契约手写的期望值。探针的两个部件**结构字段全缺**、payload 全是可识别的串——任何
        # 「用 payload 顶上某个判定字段」的写法都会在这两条断言上显形。
        first = _to_inbound_email(_probe_message(payload="第一份"))
        second = _to_inbound_email(_probe_message(payload="第二份"))

        assert first == second
        assert first == InboundEmail(
            content_type="multipart/report",
            headers={"from": _FROM},
            parts=(
                MimePart(content_type="message/delivery-status"),
                MimePart(content_type="message/rfc822"),
            ),
        )
        assert set(field.name for field in fields(MimePart)) == _MIME_PART_FIELDS
        assert set(field.name for field in fields(InboundEmail)) == {
            "content_type",
            "headers",
            "parts",
        }


# ---------------------------------------------------------------------------
# 结果对象的非零分支（`T-46` 收口遗留的第二部分）
# ---------------------------------------------------------------------------


class _StubRepos:
    """流层仓储边界的捕获型替身：只记录落库实参，判定仍在 `core/inbound` 真实现里跑。"""

    def __init__(
        self,
        *,
        targets: Mapping[str, tuple[Any, datetime | None]] | None = None,
        commit_error: bool = False,
    ) -> None:
        self.targets = {} if targets is None else dict(targets)
        self.commit_error = commit_error
        self.marked_failed: list[tuple[Any, str]] = []
        self.inserted: list[dict[str, Any]] = []
        self.cursor_advances: list[tuple[str, int, int]] = []
        self.commit_calls = 0
        self.commits = 0

    async def outbound_targets_by_message_id(
        self, session: object, message_ids: Sequence[str]
    ) -> dict[str, tuple[Any, datetime | None]]:
        return {
            message_id: self.targets[message_id]
            for message_id in message_ids
            if message_id in self.targets
        }

    async def insert_inbound_pending(self, session: object, **kwargs: Any) -> bool:
        self.inserted.append(kwargs)
        return True

    async def advance_cursor(
        self, session: object, mailbox: str, *, uidvalidity: int, uid: int, now: datetime
    ) -> object:
        self.cursor_advances.append((mailbox, uidvalidity, uid))
        return object()

    async def ensure_cursor(
        self, session: object, mailbox: str, *, uidvalidity: int, uidnext: int, now: datetime
    ) -> tuple[bool, int]:
        return False, 0

    async def mark_failed_from_sent(
        self, session: object, message_id: Any, *, error_class: str, now: datetime
    ) -> bool:
        self.marked_failed.append((message_id, error_class))
        return True


class _StubFetcher:
    """`providers/imap` 边界的替身：直接交出已解析的邮件（不碰网络）。"""

    def __init__(self, messages: Sequence[FetchedMessage]) -> None:
        self._messages = tuple(messages)

    async def read_mailbox_state(self, *, timeout_seconds: int | None = None) -> MailboxStateResult:
        return MailboxStateResult(
            state=MailboxState(mailbox=_MAILBOX, uidvalidity=_UIDVALIDITY, uidnext=99)
        )

    async def fetch_messages(
        self, *, after_uid: int, limit: int, timeout_seconds: int | None = None
    ) -> FetchResult:
        return FetchResult(messages=self._messages)


class _FakeSession:
    """会话替身：只实现流层用到的 `commit()`；按开关把提交变成一次真实 DB 故障。

    开关的判据是「第几次提交」——`design.md` §3.4 的事务口径是「游标初始化一次短事务，之后每封
    一次事务」，因此第 1 次提交成功、**之后**的提交抛 `SQLAlchemyError`：注入的正是「单封事务
    失败」那条分支，而不是把游标初始化一起打断（那会让这一条断言测错对象）。
    """

    def __init__(self, repos: _StubRepos) -> None:
        self._repos = repos

    async def commit(self) -> None:
        self._repos.commit_calls += 1
        if self._repos.commit_error and self._repos.commit_calls > 1:
            raise SQLAlchemyError("替身注入的逐封事务提交故障")
        self._repos.commits += 1


def _session_factory(repos: _StubRepos) -> fetch_inbound.SessionFactory:
    """每封一个新会话（与 `flows/cleanup.py` 的 `SessionFactory` 用法一致）。"""

    @asynccontextmanager
    async def factory() -> AsyncIterator[_FakeSession]:
        yield _FakeSession(repos)

    return cast(fetch_inbound.SessionFactory, factory)


def _settings() -> Settings:
    """配置对象：显式传参 + `_env_file=None`——单测不读 `.env`、不依赖环境（`AGENTS.md` §4）。"""
    return Settings(
        _env_file=None,
        mail_from_address="noreply@example.com",
        reply_to_address="reply@example.com",
        imap_address="reply@example.com",
    )


def _wire(
    monkeypatch: pytest.MonkeyPatch, repos: _StubRepos, messages: Sequence[FetchedMessage]
) -> None:
    """只换「IMAP 边界 + 仓储边界」，流层与判定层保持真实现。"""
    fetcher = _StubFetcher(messages)
    monkeypatch.setattr(
        fetch_inbound, "outbound_targets_by_message_id", repos.outbound_targets_by_message_id
    )
    monkeypatch.setattr(fetch_inbound, "insert_inbound_pending", repos.insert_inbound_pending)
    monkeypatch.setattr(fetch_inbound, "advance_cursor", repos.advance_cursor)
    monkeypatch.setattr(fetch_inbound, "ensure_cursor", repos.ensure_cursor)
    monkeypatch.setattr(fetch_inbound, "mark_failed_from_sent", repos.mark_failed_from_sent)
    monkeypatch.setattr(fetch_inbound, "_build_fetcher", lambda _settings: fetcher)


class TestResultNonZeroBranches:
    """`dsn_failed` / `errored` 两条非零分支真的被计数（不是恒 0 的死字段）。

    流层的这两个分支在别处只走「正常入库」与「丢弃」，计数是否接上没有任何断言兜住——把计数语句
    删掉，其余用例全绿。两条断言判的是「处置 → 字段」的对应关系（`design.md` §3.4 的处置口径）。
    """

    async def test_dsn_failed_branch_counts_and_marks_the_target(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repos = _StubRepos(targets={_MESSAGE_ID: (_TARGET_ID, _SENT_AT)})
        _wire(monkeypatch, repos, (_dsn_message(),))

        result = await fetch_inbound.fetch_inbound_tick(_session_factory(repos), _settings())

        assert result.dsn_failed == 1
        assert (result.stored, result.errored, result.fetched) == (0, 0, 1)
        assert result.discarded == {}
        # 覆写的是 DSN 指向的那一行，分类取自 `Status`（5.x.x → permanent）
        assert repos.marked_failed == [(_TARGET_ID, ERROR_CLASS_PERMANENT)]
        # 退信本身不入库
        assert repos.inserted == []

    async def test_errored_branch_counts_and_swallows_the_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repos = _StubRepos(commit_error=True)
        _wire(monkeypatch, repos, (_reply_message(),))

        result = await fetch_inbound.fetch_inbound_tick(_session_factory(repos), _settings())

        assert result.errored == 1
        assert (result.stored, result.dsn_failed, result.fetched) == (0, 0, 1)
        # 单封事务失败**不**进十一类丢弃原因（那是分拣结果；`design.md` §3.4 另记受控错误事件）。
        assert result.discarded == {}
        # 游标初始化的那一次提交照常成功：故障只落在逐封事务上（这一条测的才是目标分支）。
        assert repos.commits == 1

    def test_result_object_carries_exactly_the_designed_counters(self) -> None:
        # 两条非零分支断言的前提：这些字段名就是处置口径（`design.md` §3.4）的全部承载。
        assert set(field.name for field in fields(FetchInboundResult)) == _RESULT_FIELDS
