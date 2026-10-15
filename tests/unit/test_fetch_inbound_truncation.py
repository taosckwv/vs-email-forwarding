#!/usr/bin/env python3
"""流③（拉取入站邮件）的收件人长度口径：`_land_reply` 截断面的单测。

业务锚点（所有者终裁 T25-A，2026-10-14）：`design.md` §3.4 的「`recipient` 的长度」行与
§7 配置项清单——入站回复的 `recipient`（外部 `From` 头值）在**入库前**按配置项
`MAX_RECIPIENT_CHARS`（默认 320）截断，已否决「不设防」方向。该规则没有对应的 `AC-n`
（`plan.md` §7 按 AC 组织测试落点），因此由本文件承载它的回归可见性。

为什么直测私有函数：截断发生在 `flows/fetch_inbound.py::_land_reply` 内，位置在
「引用命中出站行 + `From` 非空 + 正文可取」之后、写待审行之前；真实路径（IMAP 拉取 → 落库）
属集成测试。`tests/unit/` 不依赖数据库与网络（`AGENTS.md` §4），所以**仓储边界**
（`outbound_targets_by_message_id` / `insert_inbound_pending`）用捕获型替身接住
（`docs/testing.md` §3.5：单测里不该触碰的边界）——替身只记录落库实参，引用解析与匹配
仍由 `core/inbound` 的真判定执行，本层不伪造判定结果。

期望值全部是手写字面量，来源不是被测代码：上限 `320`、短地址 `17` 字符、阈值 `8` 均按
`design.md` §3.4 / §7 写死；边界两侧（`== 320` 原样、`321` 截到 `320`）按
`docs/testing.md` §3.4「边界两侧都要测」各占一条。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Final, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from vs_email_forwarding.config import Settings
from vs_email_forwarding.flows import fetch_inbound
from vs_email_forwarding.providers.imap.base import FetchedMessage, FetchedPart

#: `design.md` §3.4 / §7：`MAX_RECIPIENT_CHARS` 默认 320（用例里的配置值）。
_LIMIT: Final = 320

#: 用例内把配置改成 8：证明阈值取自配置，而不是 flows 层写死的 320。
_SMALL_LIMIT: Final = 8

#: 超长 `From`：412 字符（截断后应为 320 个 `x`）。
_LONG_FROM: Final = "x" * 400 + "@example.com"

#: 恰等于上限：320 字符（边界 `==`，原样入库）。
_AT_LIMIT_FROM: Final = "x" * 308 + "@example.com"

#: 比上限多一个字符：321 字符（边界 `>`，截断后与 `_AT_LIMIT_FROM` 相同）。
_OVER_LIMIT_FROM: Final = _AT_LIMIT_FROM + "z"

#: 上限之内的短地址：17 字符（原样入库）。
_SHORT_FROM: Final = "short@example.com"

#: 出站行主键与 `sent_at`：替身按 `db/repositories/messages.py` 的返回形状给值。
_TARGET_ID: Final = uuid.UUID("11111111-1111-1111-1111-111111111111")
_SENT_AT: Final = datetime(2025, 6, 1, tzinfo=UTC)

#: 服务生成的出站 Message-ID 形状（`design.md` §3.1），用作回复邮件的 `References`。
_REFERENCE: Final = "<11111111-1111-1111-1111-111111111111@example.com>"

#: `_land_reply` 的会话参数：两个替身都不使用会话，故传哨兵（不建引擎、不连库）。
_UNTOUCHED_SESSION: Final = cast(AsyncSession, object())


class _CapturingRepositories:
    """仓储边界的捕获型替身：记下落库实参，断言全在用例侧。"""

    def __init__(self) -> None:
        self.recipient: str | None = None

    async def outbound_targets_by_message_id(
        self, session: AsyncSession, message_ids: Sequence[str]
    ) -> dict[str, tuple[uuid.UUID, datetime]]:
        # 只替「读库」这一步：引用解析与匹配仍走 core/inbound 的真判定。
        assert list(message_ids) == [_REFERENCE]
        return {_REFERENCE: (_TARGET_ID, _SENT_AT)}

    async def insert_inbound_pending(self, session: AsyncSession, **kwargs: Any) -> bool:
        self.recipient = kwargs["recipient"]
        return True


def _settings(*, max_recipient_chars: int) -> Settings:
    """构造配置对象：显式传参 + `_env_file=None`——单测不读 `.env`、不依赖环境（`AGENTS.md` §4）。"""
    return Settings(
        _env_file=None,
        mail_from_address="noreply@example.com",
        reply_to_address="reply@example.com",
        imap_address="reply@example.com",
        max_recipient_chars=max_recipient_chars,
    )


def _message(from_value: str) -> FetchedMessage:
    """一封回复邮件的中性结构：`From` 是用例的唯一变量，引用指向本服务的出站邮件。"""
    return FetchedMessage(
        mailbox="INBOX",
        uidvalidity=1,
        uid=1,
        headers={"from": from_value, "references": _REFERENCE},
        content_type="text/plain",
        parts=(
            FetchedPart(
                content_type="text/plain",
                payload="回复正文",
                dsn_status=None,
                embedded_message_id=None,
            ),
        ),
    )


async def _land_reply_capturing(
    monkeypatch: pytest.MonkeyPatch, from_value: str, *, limit: int
) -> tuple[_CapturingRepositories, object]:
    """驱动 `_land_reply`：仓储边界换成替身，阈值从配置对象取（与 tick 的传参路径一致）。"""
    repositories = _CapturingRepositories()
    monkeypatch.setattr(
        fetch_inbound,
        "outbound_targets_by_message_id",
        repositories.outbound_targets_by_message_id,
    )
    monkeypatch.setattr(
        fetch_inbound,
        "insert_inbound_pending",
        repositories.insert_inbound_pending,
    )
    settings = _settings(max_recipient_chars=limit)
    outcome = await fetch_inbound._land_reply(
        _UNTOUCHED_SESSION,
        _message(from_value),
        now=_SENT_AT,
        max_recipient_chars=settings.max_recipient_chars,
    )
    return repositories, outcome


async def test_over_limit_recipient_is_truncated_to_configured_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """412 字符的 `From` → 落库的 recipient 恰好 320 字符（`design.md` §3.4 / T25-A）。"""
    repositories, outcome = await _land_reply_capturing(monkeypatch, _LONG_FROM, limit=_LIMIT)
    assert repositories.recipient == "x" * 320
    assert outcome == fetch_inbound._Outcome.STORED


async def test_short_recipient_is_stored_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """上限之内的地址原样入库：17 字符的 `From` 一个字符都不动。"""
    repositories, outcome = await _land_reply_capturing(monkeypatch, _SHORT_FROM, limit=_LIMIT)
    assert repositories.recipient == "short@example.com"
    assert outcome == fetch_inbound._Outcome.STORED


async def test_truncation_limit_comes_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """阈值改配置即生效：上限 8 → 同一个 412 字符的 `From` 截到 8 字符。"""
    repositories, outcome = await _land_reply_capturing(monkeypatch, _LONG_FROM, limit=_SMALL_LIMIT)
    assert repositories.recipient == "x" * 8
    assert outcome == fetch_inbound._Outcome.STORED


async def test_recipient_exactly_at_limit_is_stored_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """边界 `==`：恰 320 字符原样入库（`docs/testing.md` §3.4：边界两侧都要测）。"""
    repositories, outcome = await _land_reply_capturing(monkeypatch, _AT_LIMIT_FROM, limit=_LIMIT)
    assert repositories.recipient == "x" * 308 + "@example.com"
    assert outcome == fetch_inbound._Outcome.STORED


async def test_recipient_one_char_over_limit_is_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """边界 `>`：321 字符截到 320（截断后与「恰等于上限」的值相同）。"""
    repositories, outcome = await _land_reply_capturing(monkeypatch, _OVER_LIMIT_FROM, limit=_LIMIT)
    assert repositories.recipient == "x" * 308 + "@example.com"
    assert outcome == fetch_inbound._Outcome.STORED
