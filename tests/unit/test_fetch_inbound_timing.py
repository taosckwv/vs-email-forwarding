#!/usr/bin/env python3
"""流③ 逐封取写库时刻 + tick 级编排的落库实参（spec 0001 / T-46 ②、T-63）。

`design.md` §2.5 把入站回复的入库时刻定义为「写回复行时取」，并要求「同批内先后即邮箱内
顺序」；整 tick 取一次时刻灌给全批会让同一批的行拿到**同一个**时刻，批内先后随之丢失。
本文件把「tick 循环里每封取自己的时刻」钉成断言：

* 期望值来自**注入的步进时钟**（用例自持取时源，不取自被测实现）：读数按 5 秒步进；
* 同一 tick 里两封回复落库，断言两次写入收到的时刻**严格递增**、都取自注入的读数；
* 游标推进仍用 **tick 级**时刻（取时源第一次读数）——行的时间列与 tick 级时刻必须分开；
* 反恒绿：把步进时钟换成**常量时钟**时两条必须相等，证明上面的递增断言确实由取时源驱动，
  而不是任何别的顺序来源；
* 缺省口径：不注入 `clock` 时走真时钟，落库时刻必须落在用例自己夹住的真实时间窗内、
  且带时区——「可注入」不改默认取时口径。

同文件还覆盖 tick 编排的**落库实参面**（T-63 裁决二）：tick 交给仓储的每一封的完整实参
（subject / body / body_format / mailbox / uidvalidity / uid / parent_id / now / recipient）、
游标初始化与逐封推进的实参、装配点收到的配置、正文部件的选择分支、`From` 缺失或全空白。
这些断言盯的是「编排把该传的传下去了」——捕获型替身只记录实参，判定仍走 `core/inbound`
的真实现（`docs/testing.md` §3.5）。

口径事实（卡面 ② 的边界，如实记录）：本流只写 `pending_messages` 的
`created_at` / `next_attempt_at`；`received_at` 与 `reviewed_at` 属 `messages` 回复行，
由流④ 的 `approve_inbound_pending`（`db/repositories/inbound.py`）写过审行时写入。
本用例断言的是**本流落库得到的「该封自己的写库时刻」**；该时刻如何成为接口排序键
（`received_at` 原样搬运待审行的 `created_at`，所有者裁决 A）由
`tests/integration/test_repositories.py` 的到达顺序用例在库内侧钉住。

仓储边界用捕获型替身接住：单测不碰数据库与 IMAP；引用解析与匹配仍走 `core/inbound` 的
真判定，替身只记录落库实参、不伪造判定结果。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast

import pytest

from vs_email_forwarding import constants
from vs_email_forwarding.config import Settings
from vs_email_forwarding.flows import fetch_inbound
from vs_email_forwarding.flows.cleanup import SessionFactory
from vs_email_forwarding.providers.imap.base import (
    FetchedMessage,
    FetchedPart,
    FetchResult,
    MailboxState,
    MailboxStateResult,
)

#: 注入时钟的起点：tick 级时刻（用于游标推进）就是取时源的第一次读数。
_T0: Final = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)

#: 注入时钟的步进量（本用例自己的输入，驱动替身取时源给出读数）。取 5 秒而非 1 秒，是为了
#: 让断言侧的期望值不可能「顺手引用同一个符号」：断言写 `timedelta(seconds=5)` 字面量
#: （见 `test_two_replies_in_one_tick_get_their_own_write_time`）——只改这里的步长时断言必须
#: 红，否则「步长变 → 断言随动」就是自证循环（`docs/testing.md` R2）。
_INJECTED_STEP: Final = timedelta(seconds=5)

#: 出站行主键与 `sent_at`：替身按 `db/repositories/messages.py` 的返回形状给值。
_TARGET_ID: Final = uuid.UUID("22222222-2222-2222-2222-222222222222")
_SENT_AT: Final = datetime(2025, 6, 1, tzinfo=UTC)

#: 服务生成的出站 Message-ID 形状（`design.md` §3.1），两封回复都引用它。
_REFERENCE: Final = "<22222222-2222-2222-2222-222222222222@example.com>"

#: 游标替身返回的 `last_uid`（拉取起点）：两封都大于它，因此都会被取回。
_LAST_UID: Final = 9

#: 两封回复在信箱内的 UID（升序，正是「同批内先后」的来源）。
_UIDS: Final = (10, 11)

#: 回复者在头里的地址（`design.md` §3.4：`recipient` = 回复的 `From`，原样入库）。
_FROM: Final = "user@example.com"

#: 超过 `MAX_RECIPIENT_CHARS` 文档默认值（320，`design.md` §3.4）的 `From`。
_LONG_FROM: Final = "u" * 400 + "@example.com"

#: `MAX_RECIPIENT_CHARS` 的文档默认值：断言的期望值写死成字面量，不引用配置对象、也不引用
#: 被测实现（`design.md` §3.4 的「默认 320」）。
_LIMIT_LITERAL: Final = 320


class _SteppingClock:
    """可注入取时源：每次调用给下一个读数，并记下给出过的读数。"""

    def __init__(self, start: datetime, step: timedelta) -> None:
        self._next = start
        self._step = step
        self.readings: list[datetime] = []

    def __call__(self) -> datetime:
        value = self._next
        self.readings.append(value)
        self._next = value + self._step
        return value


class _CapturingWrites:
    """仓储边界的捕获型替身：只记录落库实参，断言全在用例侧。

    除了「值」也记下**会话实参**与游标实参的每一项：把会话传成 `None`、把
    `mailbox` / `uidvalidity` / `uid` 传成 `None` 这类改写不会让替身报错（`**kwargs` 收下
    即可），只有逐项断言拦得住（T-63 裁决二）。
    """

    def __init__(self) -> None:
        self.inserted: list[dict[str, Any]] = []
        self.insert_sessions: list[object] = []
        self.lookup_sessions: list[object] = []
        self.cursor_advances: list[datetime] = []
        self.cursor_sessions: list[object] = []
        self.cursor_mailboxes: list[str] = []
        self.cursor_uidvalidities: list[int] = []
        self.cursor_uids: list[int] = []
        self.ensure_calls: list[datetime] = []
        self.ensure_sessions: list[object] = []
        self.ensure_mailboxes: list[str] = []
        self.ensure_uidvalidities: list[int] = []
        self.ensure_uidnexts: list[int] = []
        self.commits: list[int] = []

    async def outbound_targets_by_message_id(
        self, session: object, message_ids: Sequence[str]
    ) -> dict[str, tuple[uuid.UUID, datetime]]:
        self.lookup_sessions.append(session)
        # 两封都引用同一封出站邮件：两封都能命中（命中判定仍由 core/inbound 执行）。
        return {message_id: (_TARGET_ID, _SENT_AT) for message_id in message_ids}

    async def insert_inbound_pending(self, session: object, **kwargs: Any) -> bool:
        self.insert_sessions.append(session)
        self.inserted.append(kwargs)
        return True

    async def advance_cursor(
        self, session: object, mailbox: str, *, uidvalidity: int, uid: int, now: datetime
    ) -> object:
        self.cursor_sessions.append(session)
        self.cursor_mailboxes.append(mailbox)
        self.cursor_uidvalidities.append(uidvalidity)
        self.cursor_uids.append(uid)
        self.cursor_advances.append(now)
        return object()

    async def ensure_cursor(
        self, session: object, mailbox: str, *, uidvalidity: int, uidnext: int, now: datetime
    ) -> tuple[bool, int]:
        self.ensure_sessions.append(session)
        self.ensure_mailboxes.append(mailbox)
        self.ensure_uidvalidities.append(uidvalidity)
        self.ensure_uidnexts.append(uidnext)
        self.ensure_calls.append(now)
        return False, _LAST_UID


class _RecordingLogger:
    """`:mod:`structlog` 记录器的替身：只记 `(事件名, 字段)`，不做任何判定。

    事件名按「第一个位置参数」收（`logger.info("inbound_discarded", **fields)` 的调用形状）；
    被变异成不传事件名的写法会落到 `event=None`，因此「事件名对不对」也是被断言的事实。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str | None, dict[str, Any]]] = []

    def info(self, event: str | None = None, **fields: Any) -> None:
        self.calls.append((event, fields))


class _StubFetcher:
    """`providers/imap` 边界的替身：直接交出已解析的邮件（不碰网络）。

    记下调用实参，好让「拉取区间取自游标、批量与超时取自配置」（`design.md` §3.4）也落成断言；
    也记下装配点收到的配置对象（`_build_fetcher(None)` 这类改写在这里即被看见）。
    """

    def __init__(self, messages: Sequence[FetchedMessage]) -> None:
        self._messages = tuple(messages)
        self.state_timeouts: list[int | None] = []
        self.fetch_calls: list[tuple[int, int, int | None]] = []
        self.built_for: list[Settings] = []

    def record_build(self, settings: Settings) -> _StubFetcher:
        self.built_for.append(settings)
        return self

    async def read_mailbox_state(self, *, timeout_seconds: int | None = None) -> MailboxStateResult:
        self.state_timeouts.append(timeout_seconds)
        return MailboxStateResult(state=MailboxState(mailbox="INBOX", uidvalidity=1, uidnext=99))

    async def fetch_messages(
        self, *, after_uid: int, limit: int, timeout_seconds: int | None = None
    ) -> FetchResult:
        self.fetch_calls.append((after_uid, limit, timeout_seconds))
        return FetchResult(messages=self._messages)


class _FakeSession:
    """会话替身：只实现流层用到的 `commit()`。"""

    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


def _session_factory(commits: list[int]) -> SessionFactory:
    """每封一个新会话（与 `flows/cleanup.py` 的 `SessionFactory` 用法一致）。"""

    @asynccontextmanager
    async def factory() -> AsyncIterator[_FakeSession]:
        session = _FakeSession()
        yield session
        commits.append(session.commits)

    return cast(SessionFactory, factory)


def _settings() -> Settings:
    """配置对象：显式传参 + `_env_file=None`——单测不读 `.env`、不依赖环境（`AGENTS.md` §4）。"""
    return Settings(
        _env_file=None,
        mail_from_address="noreply@example.com",
        reply_to_address="reply@example.com",
        imap_address="reply@example.com",
    )


def _reply(
    *,
    uid: int,
    headers: dict[str, str] | None = None,
    parts: tuple[FetchedPart, ...] | None = None,
    content_type: str = "text/plain",
) -> FetchedMessage:
    """一封能命中出站邮件的用户回复（中性结构，`design.md` §3.4）。

    默认头只有引用与 `From`（**没有** `subject`）：主题缺失时的取值口径因此是默认路径，
    「缺主题存空串」由 `test_subject_defaults_to_empty_when_header_is_missing` 单独钉住。
    """
    if headers is None:
        headers = {"from": _FROM, "references": _REFERENCE}
    if parts is None:
        parts = (
            FetchedPart(
                content_type="text/plain",
                payload="回复正文",
                dsn_status=None,
                embedded_message_id=None,
            ),
        )
    return FetchedMessage(
        mailbox="INBOX",
        uidvalidity=1,
        uid=uid,
        headers=headers,
        content_type=content_type,
        parts=parts,
    )


async def _run_tick(
    monkeypatch: pytest.MonkeyPatch,
    *,
    clock: _SteppingClock | None = None,
    messages: Sequence[FetchedMessage] | None = None,
) -> tuple[_CapturingWrites, fetch_inbound.FetchInboundResult]:
    """驱动一次真 tick：只把「外部边界 + 仓储边界」换成替身；``clock`` 不传即走缺省真时钟。

    这里只断言**任何批次都成立**的契约：装配点收到的配置、拉取区间与批量/超时的来源、
    游标初始化那次单独的事务。批次相关的断言（几封入库、几条提交、计数结果）由各用例自己下
    ——本文件既驱动「两封都入库」，也驱动「全空白 `From` 被丢弃」「超长 `From` 被截断」
    这类批次，统一写死「两封入库」会挡住它们。
    """
    writes = _CapturingWrites()
    if messages is None:
        messages = tuple(_reply(uid=uid) for uid in _UIDS)
    fetcher = _StubFetcher(messages)
    settings = _settings()
    monkeypatch.setattr(
        fetch_inbound, "outbound_targets_by_message_id", writes.outbound_targets_by_message_id
    )
    monkeypatch.setattr(fetch_inbound, "insert_inbound_pending", writes.insert_inbound_pending)
    monkeypatch.setattr(fetch_inbound, "advance_cursor", writes.advance_cursor)
    monkeypatch.setattr(fetch_inbound, "ensure_cursor", writes.ensure_cursor)
    monkeypatch.setattr(fetch_inbound, "_build_fetcher", fetcher.record_build)
    kwargs: dict[str, Any] = {} if clock is None else {"clock": clock}
    result = await fetch_inbound.fetch_inbound_tick(
        _session_factory(writes.commits), settings, **kwargs
    )
    # 装配点收到的就是本次配置：`_build_fetcher(None)` 之类的改写在这里立刻显形。
    assert fetcher.built_for == [settings]
    # 取回区间与批量、超时的来源（`design.md` §3.4）：起点是游标、批量与超时来自配置。
    assert fetcher.fetch_calls == [(_LAST_UID, settings.batch_size, settings.imap_timeout_seconds)]
    assert fetcher.state_timeouts == [settings.imap_timeout_seconds]
    # 游标初始化单独一次事务并提交（§3.4 的事务口径）。
    assert writes.commits[0] == 1
    return writes, result


def _assert_default_batch_landed(
    writes: _CapturingWrites, result: fetch_inbound.FetchInboundResult
) -> None:
    """默认批次（两封回复都过审入库）的批次级契约：事务数、计数、逐封幂等键与落库会话。

    会话断言是 T-63 裁决二的落点：把会话传成 `None` 的改写不会让替身报错，只有在这里
    逐项对得上「同一封用的就是那个会话」才拦得住。
    """
    # 三次短事务、各提交一次：游标初始化 1 次 + 逐封各 1 次（§3.4 的事务口径）。
    assert writes.commits == [1, 1, 1]
    # 两封回复都过审入库：这一批的计数与逐封落库的幂等键。
    assert result == fetch_inbound.FetchInboundResult(fetched=len(_UIDS), stored=len(_UIDS))
    assert [written["uid"] for written in writes.inserted] == list(_UIDS)
    assert [written["parent_id"] for written in writes.inserted] == [_TARGET_ID] * len(_UIDS)
    # 每一封的关联查询、落库与游标推进用的是**同一个**会话对象（同封同事务，§3.4）。
    assert len(writes.lookup_sessions) == len(_UIDS)
    assert len(writes.insert_sessions) == len(_UIDS)
    assert len(writes.cursor_sessions) == len(_UIDS)
    assert writes.lookup_sessions == writes.insert_sessions == writes.cursor_sessions, (
        "关联 / 落库 / 游标推进必须用同一封的那个会话"
    )


async def test_two_replies_in_one_tick_get_their_own_write_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一 tick 内两封回复各取自己的写库时刻：严格递增、取自注入时钟（T-46 ②）。"""
    clock = _SteppingClock(_T0, _INJECTED_STEP)
    writes, result = await _run_tick(monkeypatch, clock=clock)

    _assert_default_batch_landed(writes, result)
    assert result.stored == 2
    times = [written["now"] for written in writes.inserted]
    assert len(times) == 2
    # 期望值写成**字面量**「5 秒」而不是 `_INJECTED_STEP`：步长是本用例自己的输入，读数由替身
    # 时钟按它生成——期望值若引用同一个符号，「改步长 → 断言随动」就成了自证循环（T-63 F3-②）。
    assert times[0] < times[1]
    assert [times[1] - times[0]] == [timedelta(seconds=5)]
    # 取自注入时钟，而不是实现自己另取的时间源。
    assert all(t in clock.readings for t in times)
    # 游标推进仍用 tick 级时刻（取时源第一次读数）——行的时间列与它分开。
    assert writes.cursor_advances == [_T0, _T0]
    assert times[0] != writes.cursor_advances[0]


async def test_constant_clock_puts_both_replies_at_one_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """反恒绿：取时源为常量时钟时两条必须相等——证明递增断言确实由取时源驱动。"""
    clock = _SteppingClock(_T0, timedelta(0))
    writes, _ = await _run_tick(monkeypatch, clock=clock)

    times = [written["now"] for written in writes.inserted]
    assert times == [_T0, _T0]


async def test_tick_level_time_is_taken_once_for_the_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """游标（初始化与逐封推进）只用 tick 级时刻：取时源读数的第一次，不随封数变。"""
    clock = _SteppingClock(_T0, _INJECTED_STEP)
    writes, _ = await _run_tick(monkeypatch, clock=clock)

    _assert_default_batch_landed(writes, fetch_inbound.FetchInboundResult(fetched=2, stored=2))
    assert writes.ensure_calls == [_T0]
    assert writes.cursor_advances == [_T0, _T0]
    # tick 级时刻只出现一次在游标上；两封各自的写库时刻是它之后的两个读数。
    assert clock.readings[0] == _T0
    assert len(clock.readings) == 3


async def test_default_time_source_is_the_real_utc_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不注入 `clock` 时走真时钟：落库时刻落在真实时间窗内且带时区（T-46 ② 缺省口径）。"""
    before = datetime.now(UTC)
    writes, _ = await _run_tick(monkeypatch)
    after = datetime.now(UTC)

    times = [written["now"] for written in writes.inserted]
    assert len(times) == 2
    # 期望值由用例自己夹住的时间窗给出，不取自被测实现。
    assert all(before <= taken <= after for taken in times)
    assert all(taken.utcoffset() == timedelta(0) for taken in times)
    # 缺省口径下同一批仍逐封各取一次（真时钟读数通常不同，此处只要求非降）。
    assert times[0] <= times[1]
    assert before <= writes.cursor_advances[0] <= after


def _html_part(*, payload: str = "HTML 正文") -> FetchedPart:
    """一个 `text/html` 正文部件（§3.4 的后备来源）。"""
    return FetchedPart(
        content_type="text/html",
        payload=payload,
        dsn_status=None,
        embedded_message_id=None,
    )


def _plain_part(*, payload: str = "回复正文") -> FetchedPart:
    """一个 `text/plain` 正文部件（§3.4 的优先来源）。"""
    return FetchedPart(
        content_type="text/plain",
        payload=payload,
        dsn_status=None,
        embedded_message_id=None,
    )


async def test_landing_arguments_carry_every_ingress_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """落库实参逐项完整：收件人 / 主题 / 正文 / 格式 / 幂等三要素 / 父行 / 时刻（T-63 裁决二）。

    断言写成**整份字典相等**：少传一个键（`insert_inbound_pending` 的替身用 `**kwargs` 收下，
    缺键不会报错）与把某个键写成 `None` 一样，都会在这里显形。
    """
    headers = {"from": _FROM, "references": _REFERENCE, "subject": "回复主题"}
    messages = tuple(_reply(uid=uid, headers=dict(headers)) for uid in _UIDS)
    clock = _SteppingClock(_T0, _INJECTED_STEP)
    writes, result = await _run_tick(monkeypatch, clock=clock, messages=messages)

    _assert_default_batch_landed(writes, result)
    assert writes.inserted == [
        {
            "recipient": _FROM,
            "subject": "回复主题",
            "body": "回复正文",
            "body_format": "text",
            "parent_id": _TARGET_ID,
            "mailbox": "INBOX",
            "uidvalidity": 1,
            "uid": uid,
            # 写库时刻取自注入时钟：第 i 封拿到第 i+1 个读数（第一次读数是 tick 级时刻）。
            "now": _T0 + timedelta(seconds=5) * (index + 1),
        }
        for index, uid in enumerate(_UIDS)
    ]


async def test_subject_defaults_to_empty_when_header_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺 `Subject` 存空串（§3.4 的取值表）：空串来自缺省值，不是「没这个键就 None」。"""
    writes, result = await _run_tick(monkeypatch)

    _assert_default_batch_landed(writes, result)
    assert [written["subject"] for written in writes.inserted] == ["", ""]


async def test_truncation_limit_is_applied_at_tick_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置的长度上限贯通到逐封落库：超长 `From` 截到 320 再入库（§3.4、`AC` 口径）。

    `_land_reply` 自身的截断面由 `test_fetch_inbound_truncation.py` 直测；这里钉的是 **tick
    把上限传下去**这条链路——上限丢在 tick 层时截断面根本收不到值。
    """
    messages = tuple(
        _reply(uid=uid, headers={"from": _LONG_FROM, "references": _REFERENCE}) for uid in _UIDS
    )
    writes, result = await _run_tick(monkeypatch, messages=messages)

    _assert_default_batch_landed(writes, result)
    assert len(_LONG_FROM) > _LIMIT_LITERAL
    assert [written["recipient"] for written in writes.inserted] == [
        _LONG_FROM[:_LIMIT_LITERAL]
    ] * len(_UIDS)


async def test_cursor_advance_carries_each_message_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """逐封推进游标的实参：信箱 / `UIDVALIDITY` / `UID` 都是这一封自己的（§3.4 的幂等三要素）。"""
    writes, result = await _run_tick(monkeypatch)

    _assert_default_batch_landed(writes, result)
    assert writes.cursor_mailboxes == ["INBOX", "INBOX"]
    assert writes.cursor_uidvalidities == [1, 1]
    assert writes.cursor_uids == list(_UIDS)


async def test_ensure_cursor_arguments_come_from_mailbox_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """游标初始化的实参：信箱与 `UIDVALIDITY` / `UIDNEXT` 原样取自 `read_mailbox_state`。"""
    writes, _ = await _run_tick(monkeypatch)

    assert writes.ensure_mailboxes == ["INBOX"]
    assert writes.ensure_uidvalidities == [1]
    assert writes.ensure_uidnexts == [99]
    # 初始化用的也是真会话，不是 `None`（`advance_cursor` 之外的第二个会话落点）。
    assert len(writes.ensure_sessions) == 1
    assert writes.ensure_sessions[0] is not None


async def test_body_selection_prefers_text_plain_and_falls_back_to_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§3.4 的正文取值表：两种部件都有 → 取 `text/plain`；只有 html → 取它并置 `html`。"""
    plain_and_html = _reply(
        uid=_UIDS[0],
        content_type="multipart/alternative",
        parts=(_html_part(), _plain_part()),
    )
    html_only = _reply(uid=_UIDS[1], content_type="text/html", parts=(_html_part(),))
    writes, result = await _run_tick(monkeypatch, messages=(plain_and_html, html_only))

    _assert_default_batch_landed(writes, result)
    assert [(written["body"], written["body_format"]) for written in writes.inserted] == [
        ("回复正文", "text"),
        ("HTML 正文", "html"),
    ]


async def test_missing_or_blank_from_is_discarded_as_no_reply_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`From` 缺失**或全空白**都按 `no_reply_from` 计数丢弃：`recipient` 是 NOT NULL（§3.4）。

    全空白这一支是「或」与「与」的分界：`raw is None or not raw.strip()` 写成 `and` 时，
    全空白头会被当成合法收件人入库。
    """
    messages = (
        _reply(uid=_UIDS[0], headers={"references": _REFERENCE}),
        _reply(uid=_UIDS[1], headers={"from": "   ", "references": _REFERENCE}),
    )
    writes, result = await _run_tick(monkeypatch, messages=messages)

    assert result == fetch_inbound.FetchInboundResult(fetched=2, discarded={"no_reply_from": 2})
    assert writes.inserted == []
    # 丢弃不影响游标推进：每一封仍推进到自己的 UID（§3.4「处理完再推」）。
    assert writes.cursor_uids == list(_UIDS)


async def test_discard_log_event_carries_the_eleven_counts_and_nothing_traceable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """丢弃只按十一类计数落日志：事件形状固定，且不含任何可回溯字段（`AC-13` / `AC-28`）。

    `design.md` §3.4 的日志口径：十一类**齐全**（本轮没出现的记 0），值只有计数；
    `AGENTS.md` §8 第 4 条的红线——发件人 / 收件人 / 主题 / 正文 / UID / 原始头值一律不得进日志。
    这条断言把「事件名 + 字段集合 + 每个字段的值」三样都钉住：多一个字段（哪怕名叫
    `recipient`）与少一类计数都算违规。

    记录方式用**替身记录器**（`monkeypatch` 换掉本模块的 `logger`），不用 structlog 的
    `capture_logs()`：应用配置里 `cache_logger_on_first_use=True`（`logging.py`），同一次
    pytest 会话里本模块的 logger 一旦被别的用例先用过，全局捕获就再也收不到它的事件——
    这条断言会随用例执行顺序时红时绿（首次引入时即在 mutmut 的 clean run 里红过）。
    替身只记录 `(事件名, 字段)`、不做判定，判定仍由下面的断言做（`docs/testing.md` §3.5）。
    """
    messages = (
        # 缺 `From` → `no_reply_from`；无文本部件 → `unparsable`：两个不同的受控原因各 1 条。
        _reply(uid=_UIDS[0], headers={"references": _REFERENCE}),
        _reply(uid=_UIDS[1], headers={"from": _FROM, "references": _REFERENCE}, parts=()),
    )
    recorder = _RecordingLogger()
    monkeypatch.setattr(fetch_inbound, "logger", recorder)

    writes, result = await _run_tick(monkeypatch, messages=messages)

    assert result == fetch_inbound.FetchInboundResult(
        fetched=2, discarded={"no_reply_from": 1, "unparsable": 1}
    )
    assert writes.inserted == []

    assert len(recorder.calls) == 1, "本轮有丢弃却只应记一条 inbound_discarded"
    event, fields = recorder.calls[0]
    assert event == "inbound_discarded"
    # 全等而不是「包含」：多出任何字段（收件人 / 主题 / UID…）都算越线。
    expected = dict.fromkeys(constants.DISCARD_REASONS, 0)
    expected.update({"no_reply_from": 1, "unparsable": 1})
    assert fields == {**expected, "total": 2}
