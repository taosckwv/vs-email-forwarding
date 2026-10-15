#!/usr/bin/env python3
"""流③ 拉取入站的集成用例（spec 0001 / `T-36` / `FR-6`~`FR-9` / `AC-10`~`AC-14` / `AC-28` / `AC-38` / `AC-39`）。

真源：`design.md` §3.4（分拣四类去向表、入站行取值表、游标逐封同事务推进、日志与错误事件口径）、§3.3
「退信到达」一行（退信覆写 `failed` 的写列口径与 `dsn_target_not_sent`）、§2.2（入站唯一键 =
`mailbox + uidvalidity + uid`）、§2.5（回复的落库时刻 = 流③ 的入库时刻）；`plan.md` §5.4（流③ 的幂等防线）、
§7.2（集成夹具与替身边界）、§7.3（本文件的 AC 落点与用例名）、§7.4（`AC-39` 的三步构造）；`spec.md` 的
`AC-10`~`AC-14` / `AC-28` / `AC-38` / `AC-39` 原文；`AGENTS.md` §8.1（幂等）/ §8.4（日志红线）。

**期望值不来自 `fetch_inbound.py`**：四类去向与写列口径取自 `design.md` §3.4 / §3.3；丢弃原因与状态取值
取自 `constants.py` 的具名常量；时刻由注入的固定时钟给出（`plan.md` §7.2：时间推进不靠 sleep）；行状态一律
用裸 `SELECT` 读回。`AC-12` / `AC-14` 的「可查询」判定走调用方视角的
`service/query_replies.query_replies`（`GET .../replies` 的服务入口），不是直接数表——「不进入可查询到的
回复集合」按可查询面判定。

**真实依赖**：PostgreSQL 是真的（`conftest.py` 的运行级隔离库 `vs_email_forwarding_test_<pid>_<hex>`），
只有两处**进程边界**是替身（`plan.md` §7.2）：IMAP 服务由 `imap_fake` 顶替（交出的仍是
`providers/imap/base.py` 的中性结构），审查器经 `providers/review/factory.py` 顶替（`fake_review`，
`AC-12` 的拒审档）——分拣判定与落库全走真实现。

**「取回顺序 = UID 升序」这条契约钉在哪**：真实承载在 `providers/imap/base.py`——`FetchResult.messages`
的字段说明（`:160`「取回并解析好的封，按 UID 升序」）与 `ImapFetcher.fetch_messages` 的 docstring（`:207`
「返回按 UID 升序——调用方靠它**逐封**『处理该封 + 把游标推到该封的 UID』同事务推进」）。本文件的
`imap_fake` 只是**镜像**该契约（`conftest.FakeImapFetcher.fetch_messages` 按 `sorted(key=uid)` 交出），
真实 `ImapClientFetcher` 的取回顺序在集成层没有运行期用例——集成测试不连真 IMAP；顺序的**后果**由本文件的
游标断言承担：`advance_cursor` 推到**该封自己的 UID**，顺序若反过来，游标会退到批内较小的 UID。

**日志断言的两条采集面**：字段级断言走 `flows/fetch_inbound.logger` 的记录器替身（确定、与用例顺序无关
——structlog 的末端渲染器由**进程级** `configure_logging` 决定，`test_send_flow.py` 的 `AC-24` 用例已登记
同一现象）；**真实渲染面**另用一个不替换记录器的用例（`capsys` + `caplog` 并集）复核「事件出现了、且正文
/ 地址 / 主题 / UID 一个都不在里面」。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vs_email_forwarding.config import Settings
from vs_email_forwarding.constants import (
    DIRECTION_INBOUND,
    DIRECTION_OUTBOUND,
    DISCARD_REASONS,
    ERROR_CLASS_PERMANENT,
    ERROR_CLASS_TRANSIENT,
    MESSAGE_STATE_FAILED,
    MESSAGE_STATE_SENDING,
    MESSAGE_STATE_SENT,
    REJECT_SOURCE_REVIEW_REJECTED,
)
from vs_email_forwarding.db.models import ImapCursor, Message, PendingMessage, RejectedMessage
from vs_email_forwarding.flows import fetch_inbound
from vs_email_forwarding.flows.fetch_inbound import FetchInboundResult, fetch_inbound_tick
from vs_email_forwarding.flows.review_inbound import review_inbound_tick
from vs_email_forwarding.providers.imap.base import FetchedPart
from vs_email_forwarding.service.query_replies import query_replies

pytestmark = pytest.mark.integration

#: 本次 tick 的注入时刻（`plan.md` §7.2：时间推进不靠 sleep）。
_NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)

#: 出站行的发出时刻：**刻意与 `_NOW` 不同**——退信覆写的是「已发出」的行，两个时刻必须可分辨。
_SENT_AT = datetime(2025, 5, 31, 9, 0, 0, tzinfo=UTC)

#: 尚未发出的行上的重试时刻：`AC-38` 要求它**原样不动**，因此取一个与 `_NOW` 无关的未来值。
_FUTURE_ATTEMPT = datetime(2025, 6, 2, 6, 0, 0, tzinfo=UTC)

#: 夹具内容一律 `example.com` 占位（`AGENTS.md` §8 第 4 条、`docs/testing.md` §4.7）。
_MAILBOX = "INBOX"
_UIDVALIDITY = 4242
_RECIPIENT = "colleague@example.com"
_OUTBOUND_SUBJECT = "出站父行主题"
_REPLY_FROM = "replier@example.com"
_REPLY_SUBJECT = "回复夹具主题"
_REPLY_BODY = "回复夹具正文"
_MDN_FROM = "reader@example.com"

#: 丢弃用例的 UID 取**不易撞上的大数**：日志泄漏断言要按十进制串扫描文本，数字太小会撞上计数值。
_DISCARD_UIDS = (987654, 987655, 987656)

#: 三个不相关封的发件人与主题：与 UID 一起构成「不得出现在日志里」的标记集合。
_DISCARD_MARKERS = (
    ("discard-one@example.com", "丢弃夹具主题一", "丢弃夹具正文一"),
    ("discard-two@example.com", "丢弃夹具主题二", "丢弃夹具正文二"),
    ("discard-three@example.com", "丢弃夹具主题三", "丢弃夹具正文三"),
)

_Factory = async_sessionmaker[AsyncSession]


def _fixed_clock() -> datetime:
    """注入的时钟（`plan.md` §7.2）：同一次 tick 内恒定，因此行的时刻可逐字段断言。"""
    return _NOW


async def _rows(factory: _Factory, statement: Any) -> list[dict[str, Any]]:
    """执行一条只读语句并把结果摊成普通映射（用例的读路径走裸 `SELECT`，不经被测代码）。"""
    async with factory() as session:
        result = await session.execute(statement)
    return [dict(row._mapping) for row in result.all()]


async def _table_counts(factory: _Factory) -> dict[str, int]:
    """三张邮件表的行数（`AC-13` 的「均不增」按这张快照的前后对比判定）。"""
    counts: dict[str, int] = {}
    for name, model in (
        ("messages", Message),
        ("pending_messages", PendingMessage),
        ("rejected_messages", RejectedMessage),
    ):
        rows = await _rows(factory, sa.select(sa.func.count()).select_from(model))
        counts[name] = rows[0]["count"]
    return counts


async def _seed_outbound(
    factory: _Factory,
    *,
    status: str,
    sent_at: datetime | None,
    attempt_count: int = 3,
    next_attempt_at: datetime = _NOW,
) -> tuple[uuid.UUID, str]:
    """放一行出站邮件，返回 `(主键, Message-ID)`；`Message-ID` 是入站侧关联的唯一钥匙。

    非终态的 `sending` 行刻意带上非零尝试计数与未来重试时刻：`AC-38` 判的就是这两列**原样不动**。
    """
    row_id = uuid.uuid4()
    message_id = f"<{row_id}@example.com>"
    async with factory() as session:
        await session.execute(
            sa.insert(Message).values(
                id=row_id,
                direction=DIRECTION_OUTBOUND,
                status=status,
                message_id=message_id,
                recipient=_RECIPIENT,
                subject=_OUTBOUND_SUBJECT,
                body="出站父行正文",
                body_format="text",
                attempt_count=attempt_count,
                next_attempt_at=next_attempt_at,
                created_at=_NOW,
                sent_at=sent_at,
                reviewed_at=_NOW,
            )
        )
        await session.commit()
    return row_id, message_id


async def _outbound_row(factory: _Factory, row_id: uuid.UUID) -> dict[str, Any] | None:
    rows = await _rows(
        factory,
        sa.select(
            Message.status,
            Message.last_error_class,
            Message.sent_at,
            Message.attempt_count,
            Message.next_attempt_at,
        ).where(Message.id == row_id),
    )
    return rows[0] if rows else None


async def _pending_rows(factory: _Factory) -> list[dict[str, Any]]:
    return await _rows(
        factory,
        sa.select(
            PendingMessage.id,
            PendingMessage.direction,
            PendingMessage.recipient,
            PendingMessage.subject,
            PendingMessage.body,
            PendingMessage.body_format,
            PendingMessage.parent_id,
            PendingMessage.mailbox,
            PendingMessage.uidvalidity,
            PendingMessage.uid,
            PendingMessage.attempt_count,
            PendingMessage.next_attempt_at,
            PendingMessage.created_at,
        ),
    )


async def _rejected_rows(factory: _Factory) -> list[dict[str, Any]]:
    return await _rows(
        factory,
        sa.select(
            RejectedMessage.id,
            RejectedMessage.direction,
            RejectedMessage.source,
            RejectedMessage.subject,
            RejectedMessage.body,
        ),
    )


async def _cursor(factory: _Factory, mailbox: str) -> dict[str, Any] | None:
    rows = await _rows(
        factory,
        sa.select(ImapCursor.uidvalidity, ImapCursor.last_uid).where(ImapCursor.mailbox == mailbox),
    )
    return rows[0] if rows else None


async def _rewind_cursor(factory: _Factory, mailbox: str, last_uid: int) -> None:
    """把游标拨回去：同一封会被再取一次——真实 IMAP 就是这个行为（`conftest.FakeImapFetcher` 的说明）。

    游标在库里、不在假 IMAP 里，所以「同一 `mailbox + uidvalidity + uid` 再来一遍」由用例直接构造
    （`AC-11` / `AC-39`，`plan.md` §7.4）。
    """
    async with factory() as session:
        await session.execute(
            sa.update(ImapCursor).where(ImapCursor.mailbox == mailbox).values(last_uid=last_uid)
        )
        await session.commit()


async def _prime_cursor(factory: _Factory, settings: Settings, imap_fake: Any) -> None:
    """把信箱摆成「首见」并让游标初始化：**本轮一封都不拉**（`design.md` §3.4 已登记的代价）。

    必须**先**跑这一轮再放邮件：`ensure_cursor` 首见信箱时把 `last_uid` 写成「当时」的
    `uidnext - 1`，先放邮件再跑的话，那一次初始化会把这些邮件整批跳过
    （`TestCursorSemantics.test_first_tick_skips_what_is_already_in_the_mailbox` 钉住这条）。
    """
    imap_fake.rotate_uidvalidity(uidvalidity=_UIDVALIDITY, uidnext=1)
    await _tick(factory, settings)


async def _tick(factory: _Factory, settings: Settings) -> FetchInboundResult:
    """跑一次流③（时钟固定，见 `_fixed_clock`）。"""
    return await fetch_inbound_tick(factory, settings, clock=_fixed_clock)


def _part(
    content_type: str,
    *,
    payload: str | None = None,
    dsn_status: str | None = None,
    embedded_message_id: str | None = None,
) -> FetchedPart:
    return FetchedPart(
        content_type=content_type,
        payload=payload,
        dsn_status=dsn_status,
        embedded_message_id=embedded_message_id,
    )


def _dsn_sample(
    imap_fake: Any, *, original_message_id: str, status: str = "5.1.1", uid: int = 1
) -> None:
    """往信箱里放一封中性结构形态的退信：`multipart/report` + `delivery-status` + 内嵌原邮件。"""
    imap_fake.add_message(
        uid=uid,
        content_type="multipart/report",
        headers={"from": "mailer-daemon@example.com"},
        parts=(
            _part("message/delivery-status", dsn_status=status),
            _part("message/rfc822", embedded_message_id=original_message_id),
        ),
    )


def _mdn_sample(imap_fake: Any, *, uid: int = 1) -> None:
    """往信箱里放一封阅读回执：与退信**同容器**，只有部件不同（`design.md` §3.4）。"""
    imap_fake.add_message(
        uid=uid,
        content_type="multipart/report",
        headers={"from": _MDN_FROM, "subject": "阅读回执"},
        parts=(_part("message/disposition-notification"),),
    )


def _reply_sample(
    imap_fake: Any,
    *,
    uid: int,
    reference: str,
    from_address: str = _REPLY_FROM,
    subject: str = _REPLY_SUBJECT,
    body: str = _REPLY_BODY,
) -> None:
    """往信箱里放一封普通回复（中性结构；引用头决定它能否关联回出站行）。"""
    imap_fake.add_message(
        uid=uid,
        content_type="text/plain",
        headers={"from": from_address, "in-reply-to": reference, "subject": subject},
        parts=(_part("text/plain", payload=body),),
    )


class _RecordingLogger:
    """`flows/fetch_inbound` 的记录器替身：只记 `(事件名, 字段)`，不做任何判定。

    事件名按「第一个位置参数」收（`logger.info("inbound_discarded", **fields)` 的调用形状）；
    被改写成不传事件名的写法会落到 `event=None`，因此「事件名对不对」也是被断言的事实。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str | None, dict[str, Any]]] = []

    def info(self, event: str | None = None, **fields: Any) -> None:
        self.calls.append((event, fields))

    def warning(self, event: str | None = None, **fields: Any) -> None:
        self.calls.append((event, fields))

    def named(self, event: str) -> list[dict[str, Any]]:
        """本次 tick 里事件名等于 `event` 的全部字段映射。"""
        return [fields for name, fields in self.calls if name == event]


@pytest.fixture
def recorded_logs(monkeypatch: pytest.MonkeyPatch) -> _RecordingLogger:
    """记录流层事件（字段级断言的采集面；真实渲染面见 `TestRealLogSurface`）。"""
    recorder = _RecordingLogger()
    monkeypatch.setattr(fetch_inbound, "logger", recorder)
    return recorder


@dataclass(frozen=True)
class _MailboxFixture:
    """本文件反复用到的一小撮夹具：隔离库会话工厂、配置、假信箱。"""

    factory: _Factory
    settings: Settings
    imap_fake: Any


@pytest.fixture
async def fixture_bundle(
    session_factory: _Factory, integration_environment: Settings, imap_fake: Any
) -> _MailboxFixture:
    """把三个夹具打包（不改变它们的语义），并让信箱进入「首见 + 游标已初始化」状态。"""
    await _prime_cursor(session_factory, integration_environment, imap_fake)
    return _MailboxFixture(
        factory=session_factory, settings=integration_environment, imap_fake=imap_fake
    )


@pytest.fixture
def snapshot_mailbox(
    session_factory: _Factory, integration_environment: Settings, imap_fake: Any
) -> _MailboxFixture:
    """信箱停在「首见」状态（**不**预跑初始化那一轮）：供游标初始化用例使用。"""
    imap_fake.rotate_uidvalidity(uidvalidity=_UIDVALIDITY, uidnext=1)
    return _MailboxFixture(
        factory=session_factory, settings=integration_environment, imap_fake=imap_fake
    )


class TestDsnSetsFailed:
    """`AC-10`：退信把内嵌原邮件指向的**已发出**行置 `failed`，退信本身不入库。"""

    async def test_dsn_sets_failed(self, fixture_bundle: _MailboxFixture) -> None:
        outbound_id, message_id = await _seed_outbound(
            fixture_bundle.factory,
            status=MESSAGE_STATE_SENT,
            sent_at=_SENT_AT,
            attempt_count=1,
            next_attempt_at=_SENT_AT,
        )
        _dsn_sample(fixture_bundle.imap_fake, original_message_id=message_id, status="5.1.1")
        before = await _table_counts(fixture_bundle.factory)

        result = await _tick(fixture_bundle.factory, fixture_bundle.settings)

        assert result.fetched == 1
        assert result.dsn_failed == 1
        assert (result.stored, result.errored) == (0, 0)
        assert result.discarded == {}  # 置失败不是丢弃

        row = await _outbound_row(fixture_bundle.factory, outbound_id)
        assert row is not None
        assert row["status"] == MESSAGE_STATE_FAILED
        # `design.md` §3.3：5.x.x → permanent；4.x.x → transient；取不出分类复用 internal。
        assert row["last_error_class"] == ERROR_CLASS_PERMANENT
        # 退信不写 `sent_at`（已发出的事实不因退信而消失）、不碰尝试列（结果事务一律不碰）。
        assert row["sent_at"] == _SENT_AT
        assert (row["attempt_count"], row["next_attempt_at"]) == (1, _SENT_AT)
        # 退信本身不入库：三张表里只有原来那一条出站行。
        assert await _table_counts(fixture_bundle.factory) == before

    async def test_dsn_transient_status_lands_transient(
        self, fixture_bundle: _MailboxFixture
    ) -> None:
        # 4.x.x 的分类是同一条写列口径的另一半：分类取错了上面那条也照样绿。
        outbound_id, message_id = await _seed_outbound(
            fixture_bundle.factory, status=MESSAGE_STATE_SENT, sent_at=_SENT_AT
        )
        _dsn_sample(fixture_bundle.imap_fake, original_message_id=message_id, status="4.4.1")

        result = await _tick(fixture_bundle.factory, fixture_bundle.settings)

        assert result.dsn_failed == 1
        row = await _outbound_row(fixture_bundle.factory, outbound_id)
        assert row is not None
        assert row["last_error_class"] == ERROR_CLASS_TRANSIENT


class TestIdempotency:
    """`AC-11` / `AC-39`：入站唯一键拦重复，且撞键**不得把游标推进一起回滚**（`AGENTS.md` §8.1）。"""

    async def test_inbound_dedup_by_uid(self, fixture_bundle: _MailboxFixture) -> None:
        outbound_id, message_id = await _seed_outbound(
            fixture_bundle.factory, status=MESSAGE_STATE_SENT, sent_at=_SENT_AT
        )
        _reply_sample(fixture_bundle.imap_fake, uid=1, reference=message_id)

        first = await _tick(fixture_bundle.factory, fixture_bundle.settings)
        assert (first.fetched, first.stored, first.discarded) == (1, 1, {})

        rows = await _pending_rows(fixture_bundle.factory)
        assert len(rows) == 1
        # 入站幂等键三要素（`design.md` §2.2）+ 关联到的父行主键。
        assert (rows[0]["mailbox"], rows[0]["uidvalidity"], rows[0]["uid"]) == (
            fixture_bundle.settings.imap_mailbox,
            _UIDVALIDITY,
            1,
        )
        assert rows[0]["parent_id"] == outbound_id
        assert rows[0]["direction"] == DIRECTION_INBOUND

        # 同一 `mailbox + uidvalidity + uid` 再来一遍：真实 IMAP 在游标回退后会重发同一封。
        settled = await _table_counts(fixture_bundle.factory)
        await _rewind_cursor(
            fixture_bundle.factory, fixture_bundle.settings.imap_mailbox, last_uid=0
        )
        second = await _tick(fixture_bundle.factory, fixture_bundle.settings)

        assert (second.fetched, second.stored) == (1, 0)
        assert second.discarded == {"duplicate": 1}
        # 库里只有原来那一条（三张表都不增），且它还是同一封。
        assert await _table_counts(fixture_bundle.factory) == settled
        assert [row["uid"] for row in await _pending_rows(fixture_bundle.factory)] == [1]

    async def test_cursor_rollback_advances(self, fixture_bundle: _MailboxFixture) -> None:
        """`AC-39` 三步（`plan.md` §7.4）：正常处理一封 → 回退游标 → 再拉一次。

        ① 库里没有第二条记录（幂等键拦住）② `imap_cursors.last_uid` 仍然推进到该封的 UID
        ③ 本次计一条 `duplicate`。第②步是这条 AC 的刀刃：冲突若把推进一起回滚，游标会停在回退值上，
        每一轮都重撞同一封、入站永久停摆。
        """
        _, message_id = await _seed_outbound(
            fixture_bundle.factory, status=MESSAGE_STATE_SENT, sent_at=_SENT_AT
        )
        _reply_sample(fixture_bundle.imap_fake, uid=1, reference=message_id)
        mailbox = fixture_bundle.settings.imap_mailbox

        await _tick(fixture_bundle.factory, fixture_bundle.settings)
        cursor = await _cursor(fixture_bundle.factory, mailbox)
        assert cursor is not None
        assert cursor["last_uid"] == 1
        stored = await _table_counts(fixture_bundle.factory)

        await _rewind_cursor(fixture_bundle.factory, mailbox, last_uid=0)
        assert (await _cursor(fixture_bundle.factory, mailbox))["last_uid"] == 0

        result = await _tick(fixture_bundle.factory, fixture_bundle.settings)

        assert result.discarded == {"duplicate": 1}
        assert await _table_counts(fixture_bundle.factory) == stored
        advanced = await _cursor(fixture_bundle.factory, mailbox)
        assert advanced is not None
        assert advanced["last_uid"] == 1  # ② 推进照常提交（同一个事务，不被冲突回滚）
        assert advanced["uidvalidity"] == _UIDVALIDITY  # 轮换才重置，这里不动


class TestReplyLanding:
    """`AC-12` / `AC-14`：回复落待审行、经流④ 审查后进/不进可查询的回复集合。"""

    async def test_rejected_reply_not_queryable(
        self, fixture_bundle: _MailboxFixture, fake_review: Any
    ) -> None:
        """`AC-12` 的**流③ → 流④ 全程**变体（兄弟文件覆盖「已有一行待审行」的半程）。

        这里从信箱起跑：拉取 → 分拣 → 落待审行 → 恒拒审 → 该回复不进调用方可查询的回复集合。
        """
        outbound_id, message_id = await _seed_outbound(
            fixture_bundle.factory, status=MESSAGE_STATE_SENT, sent_at=_SENT_AT
        )
        _reply_sample(fixture_bundle.imap_fake, uid=1, reference=message_id)
        fake_review.mode = "reject"

        fetched = await _tick(fixture_bundle.factory, fixture_bundle.settings)
        assert (fetched.stored, fetched.discarded) == (1, {})

        reviewed = await review_inbound_tick(
            fixture_bundle.factory, fixture_bundle.settings, now=_NOW
        )
        assert reviewed.rejected == 1
        assert (reviewed.approved, reviewed.errored) == (0, 0)

        rejected = await _rejected_rows(fixture_bundle.factory)
        assert len(rejected) == 1
        assert rejected[0]["source"] == REJECT_SOURCE_REVIEW_REJECTED
        assert rejected[0]["direction"] == DIRECTION_INBOUND
        # 入站被拒行带上内容供复核（`design.md` §2.1），但它不在可查询面里。
        assert (rejected[0]["subject"], rejected[0]["body"]) == (_REPLY_SUBJECT, _REPLY_BODY)
        assert await _pending_rows(fixture_bundle.factory) == []

        page = await query_replies(outbound_id)
        assert page.found is True  # 父行仍在：找不到回复不是「ID 不存在」
        assert page.replies == []

    async def test_mdn_dropped_reply_kept(
        self, fixture_bundle: _MailboxFixture, fake_review: Any
    ) -> None:
        """`AC-14`：回执按 `mdn` 剔除，同一批里那封正常回复照常入库并可查询。"""
        outbound_id, message_id = await _seed_outbound(
            fixture_bundle.factory, status=MESSAGE_STATE_SENT, sent_at=_SENT_AT
        )
        _mdn_sample(fixture_bundle.imap_fake, uid=1)
        _reply_sample(fixture_bundle.imap_fake, uid=2, reference=message_id)

        result = await _tick(fixture_bundle.factory, fixture_bundle.settings)

        assert (result.fetched, result.stored) == (2, 1)
        assert result.discarded == {"mdn": 1}
        assert result.errored == 0

        approved = await review_inbound_tick(
            fixture_bundle.factory, fixture_bundle.settings, now=_NOW
        )
        assert (approved.approved, approved.rejected) == (1, 0)

        # 回执没有造成任何一行：待审表与回复集合里各只有那封回复。
        assert [row["uid"] for row in await _pending_rows(fixture_bundle.factory)] == []
        page = await query_replies(outbound_id)
        assert [reply.subject for reply in page.replies] == [_REPLY_SUBJECT]
        # 排序键 = 流③ 的落库时刻（`design.md` §2.5）：注入时钟给出，不是审查时刻、不是真时钟。
        assert page.replies[0].received_at == _NOW
        assert page.replies[0].body == _REPLY_BODY

    async def test_mdn_is_not_stored_even_with_a_valid_reference(
        self, fixture_bundle: _MailboxFixture
    ) -> None:
        # 回执同时带着能命中出站行的引用：判据是**部件**，不是引用（`design.md` §3.4 的四类去向表）。
        outbound_id, message_id = await _seed_outbound(
            fixture_bundle.factory, status=MESSAGE_STATE_SENT, sent_at=_SENT_AT
        )
        fixture_bundle.imap_fake.add_message(
            uid=1,
            content_type="multipart/report",
            headers={"from": _MDN_FROM, "in-reply-to": message_id, "subject": "阅读回执"},
            parts=(_part("message/disposition-notification"),),
        )

        result = await _tick(fixture_bundle.factory, fixture_bundle.settings)

        assert result.discarded == {"mdn": 1}
        assert (result.stored, result.dsn_failed) == (0, 0)
        assert await _pending_rows(fixture_bundle.factory) == []
        assert (await query_replies(outbound_id)).replies == []


class TestDiscards:
    """`AC-13` / `AC-28`：关联不上的入站邮件丢弃不入库，日志只记条数。"""

    async def test_unmatched_inbound_discarded(
        self, fixture_bundle: _MailboxFixture, recorded_logs: _RecordingLogger
    ) -> None:
        # 引用指向一个本服务从未发出过的 Message-ID（甚至库里没有任何出站行）→ `no_match`。
        _reply_sample(fixture_bundle.imap_fake, uid=1, reference="<never-sent@example.com>")
        before = await _table_counts(fixture_bundle.factory)

        result = await _tick(fixture_bundle.factory, fixture_bundle.settings)

        assert (result.fetched, result.stored, result.dsn_failed) == (1, 0, 0)
        assert result.discarded == {"no_match": 1}
        # 三张邮件表均无新增行。
        assert await _table_counts(fixture_bundle.factory) == before

        events = recorded_logs.named("inbound_discarded")
        assert len(events) == 1  # 有丢弃时记一条，不是逐封各记一条
        assert events[0]["total"] == 1
        assert events[0]["no_match"] == 1
        # 字段是全部十一类（没出现的记 0）——采集端不必区分「没有这类」与「字段缺失」。
        assert set(events[0]) == set(DISCARD_REASONS) | {"total"}
        assert [events[0][reason] for reason in DISCARD_REASONS if reason != "no_match"] == [0] * 10

    async def test_discard_log_only_counts(
        self, fixture_bundle: _MailboxFixture, recorded_logs: _RecordingLogger
    ) -> None:
        """`AC-28`：连丢三封 → 一条合计 3 的计数记录，且事件里只有计数（无地址 / 主题 / 正文 / UID）。"""
        imap_fake = fixture_bundle.imap_fake
        for uid, (from_address, subject, body) in zip(_DISCARD_UIDS, _DISCARD_MARKERS, strict=True):
            imap_fake.add_message(
                uid=uid,
                content_type="text/plain",
                headers={
                    "from": from_address,
                    "in-reply-to": "<never-sent@example.com>",
                    "subject": subject,
                },
                parts=(_part("text/plain", payload=body),),
            )
        before = await _table_counts(fixture_bundle.factory)

        result = await _tick(fixture_bundle.factory, fixture_bundle.settings)

        assert (result.fetched, result.stored) == (3, 0)
        assert result.discarded == {"no_match": 3}
        assert await _table_counts(fixture_bundle.factory) == before

        events = recorded_logs.named("inbound_discarded")
        assert len(events) == 1
        assert events[0]["total"] == 3
        assert events[0]["no_match"] == 3
        assert set(events[0]) == set(DISCARD_REASONS) | {"total"}
        # 「只有计数」按类型判：任何字符串字段（地址 / 主题 / 正文 / UID 原文）都会让这条失败。
        assert all(isinstance(value, int) for value in events[0].values())

    async def test_duplicate_and_unmatched_share_one_event(
        self, fixture_bundle: _MailboxFixture, recorded_logs: _RecordingLogger
    ) -> None:
        # 一条事件承载整批的十一类计数：两类各一封 → total 2（不是每条丢弃一行、也不是两条事件）。
        _, message_id = await _seed_outbound(
            fixture_bundle.factory, status=MESSAGE_STATE_SENT, sent_at=_SENT_AT
        )
        _reply_sample(fixture_bundle.imap_fake, uid=1, reference=message_id)
        await _tick(fixture_bundle.factory, fixture_bundle.settings)
        mailbox = fixture_bundle.settings.imap_mailbox
        await _rewind_cursor(fixture_bundle.factory, mailbox, last_uid=0)
        _reply_sample(fixture_bundle.imap_fake, uid=2, reference="<never-sent@example.com>")
        recorded_logs.calls.clear()

        result = await _tick(fixture_bundle.factory, fixture_bundle.settings)

        assert result.discarded == {"no_match": 1, "duplicate": 1}
        events = recorded_logs.named("inbound_discarded")
        assert len(events) == 1
        assert (events[0]["total"], events[0]["duplicate"], events[0]["no_match"]) == (2, 1, 1)


class TestDsnTargetNotSent:
    """`AC-38`：`sending` + `sent_at` 为空的行收到退信 → 不改状态、不动重试列，计一条丢弃。"""

    async def test_dsn_target_not_sent(self, fixture_bundle: _MailboxFixture) -> None:
        outbound_id, message_id = await _seed_outbound(
            fixture_bundle.factory,
            status=MESSAGE_STATE_SENDING,
            sent_at=None,
            attempt_count=2,
            next_attempt_at=_FUTURE_ATTEMPT,
        )
        _dsn_sample(fixture_bundle.imap_fake, original_message_id=message_id, status="5.1.1")
        before = await _table_counts(fixture_bundle.factory)

        result = await _tick(fixture_bundle.factory, fixture_bundle.settings)

        assert (result.fetched, result.dsn_failed) == (1, 0)
        assert result.discarded == {"dsn_target_not_sent": 1}

        row = await _outbound_row(fixture_bundle.factory, outbound_id)
        assert row is not None
        # 尚未发出的行：不置失败（不可逆的错判），也不打断它自己的重试。
        assert row["status"] == MESSAGE_STATE_SENDING
        assert row["last_error_class"] is None
        assert row["sent_at"] is None
        assert (row["attempt_count"], row["next_attempt_at"]) == (2, _FUTURE_ATTEMPT)
        assert await _table_counts(fixture_bundle.factory) == before

    async def test_dsn_without_embedded_id_is_discarded(
        self, fixture_bundle: _MailboxFixture
    ) -> None:
        # 取不出内嵌原邮件标识 → `dsn_without_id`（`design.md` §3.4 登记的已知缺失），不碰任何行。
        outbound_id, _ = await _seed_outbound(
            fixture_bundle.factory, status=MESSAGE_STATE_SENT, sent_at=_SENT_AT
        )
        fixture_bundle.imap_fake.add_message(
            uid=1,
            content_type="multipart/report",
            headers={"from": "mailer-daemon@example.com"},
            parts=(_part("message/delivery-status", dsn_status="5.1.1"),),
        )

        result = await _tick(fixture_bundle.factory, fixture_bundle.settings)

        assert result.discarded == {"dsn_without_id": 1}
        assert (result.dsn_failed, result.stored) == (0, 0)
        # 那封已发出的行原样不动：弃信与置失败的分界就在"有没有可关联的原邮件标识"。
        row = await _outbound_row(fixture_bundle.factory, outbound_id)
        assert row is not None
        assert (row["status"], row["sent_at"]) == (MESSAGE_STATE_SENT, _SENT_AT)


class TestCursorSemantics:
    """流③ 的游标口径（`design.md` §3.4；非 `AC-n` 直接映射，属支撑用例）。

    单独列出来是因为它是本文件其他用例的**前提**：首见信箱那一轮一封都不拉，所以「同一封再拉一次」
    必须由用例自己在库里拨游标来构造（`AC-11` / `AC-39`）。
    """

    async def test_first_tick_skips_what_is_already_in_the_mailbox(
        self, snapshot_mailbox: _MailboxFixture
    ) -> None:
        # 首见信箱且此刻已有一封：游标被初始化成「当时」的 `UIDNEXT - 1`，这一封要等下一条新邮件。
        _reply_sample(snapshot_mailbox.imap_fake, uid=1, reference="<never-sent@example.com>")

        result = await _tick(snapshot_mailbox.factory, snapshot_mailbox.settings)

        assert (result.fetched, result.stored, result.discarded) == (0, 0, {})
        cursor = await _cursor(snapshot_mailbox.factory, snapshot_mailbox.settings.imap_mailbox)
        assert cursor is not None
        assert (cursor["uidvalidity"], cursor["last_uid"]) == (_UIDVALIDITY, 1)

    async def test_mailbox_state_failure_leaves_cursor_untouched(
        self, fixture_bundle: _MailboxFixture, recorded_logs: _RecordingLogger
    ) -> None:
        mailbox = fixture_bundle.settings.imap_mailbox
        settled = await _cursor(fixture_bundle.factory, mailbox)
        _reply_sample(fixture_bundle.imap_fake, uid=1, reference="<never-sent@example.com>")
        fixture_bundle.imap_fake.fail_with("transient")

        result = await _tick(fixture_bundle.factory, fixture_bundle.settings)

        assert (result.fetched, result.stored, result.errored) == (0, 0, 0)
        assert await _cursor(fixture_bundle.factory, mailbox) == settled
        # 连接失败额外记一条受控错误事件：只有阶段与受控分类（`design.md` §3.4）。
        assert recorded_logs.named("inbound_fetch_failed") == [
            {"stage": "mailbox_state", "error_class": ERROR_CLASS_TRANSIENT}
        ]
        assert recorded_logs.named("inbound_discarded") == []


class TestRealLogSurface:
    """`AC-28` 的采集面复核：事件在**真实渲染路径**上出现，且可回溯内容一个都不在里面。"""

    async def test_discard_event_is_rendered_without_traceable_content(
        self,
        fixture_bundle: _MailboxFixture,
        capsys: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        imap_fake = fixture_bundle.imap_fake
        for uid, (from_address, subject, body) in zip(_DISCARD_UIDS, _DISCARD_MARKERS, strict=True):
            imap_fake.add_message(
                uid=uid,
                content_type="text/plain",
                headers={
                    "from": from_address,
                    "in-reply-to": "<never-sent@example.com>",
                    "subject": subject,
                },
                parts=(_part("text/plain", payload=body),),
            )

        result = await _tick(fixture_bundle.factory, fixture_bundle.settings)
        assert result.discarded == {"no_match": 3}

        # 采集面取三条的并集：structlog 的末端渲染器由进程级 `configure_logging` 决定，而集成跑里
        # 是否调用过它取决于用例顺序（`test_send_flow.py` 的 `AC-24` 用例已登记同一现象）。
        captured = capsys.readouterr()
        text = "\n".join((captured.out, captured.err, caplog.text))

        assert "inbound_discarded" in text  # 非空转正控：事件真的落到了采集面
        for marker in _leak_markers():
            assert marker not in text

    async def test_mailbox_failure_event_is_rendered_with_its_controlled_class(
        self,
        fixture_bundle: _MailboxFixture,
        capsys: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        fixture_bundle.imap_fake.fail_with("permanent")

        await _tick(fixture_bundle.factory, fixture_bundle.settings)

        captured = capsys.readouterr()
        text = "\n".join((captured.out, captured.err, caplog.text))
        assert "inbound_fetch_failed" in text
        for marker in _leak_markers():
            assert marker not in text


def _leak_markers() -> tuple[str, ...]:
    """不得出现在日志里的可回溯标记：发件人地址、主题、正文、UID 的十进制串。"""
    markers: list[str] = [str(uid) for uid in _DISCARD_UIDS]
    for from_address, subject, body in _DISCARD_MARKERS:
        markers.extend((from_address, subject, body))
    return tuple(markers)
