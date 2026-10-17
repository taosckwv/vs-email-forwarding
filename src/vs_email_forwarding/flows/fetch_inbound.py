#!/usr/bin/env python3
"""流③ 拉取入站：取回 → 分拣 → 落库 / 置失败 / 计数丢弃（`specs/0001-email-forwarding-service` T-25）。

设计真源：``design.md`` §3.4（分拣四类去向、入站行取值表、游标逐封同事务推进、日志与错误事件口径）、
§2.2（入站唯一键 = ``mailbox + uidvalidity + uid``）、§3.3「退信到达」一行（退信的写列口径与
``internal`` 复用裁决）；``plan.md`` §5.4（流③ 的幂等防线 = 入站唯一键 + 游标同事务推进）。
``AC-10`` / ``AC-11`` / ``AC-13`` / ``AC-14`` / ``AC-28`` / ``AC-38`` / ``AC-39`` 的集成落点在
阶段 5（``tests/integration/test_fetch_inbound_flow.py``，T-36）——按 ``plan.md`` §7.1 的分工，
流的推进与幂等属集成用例，本模块不带单测。

一次 tick 的步骤（流③ **没有认领面**：增量进度由 ``imap_cursors`` 决定，不存在待认领的行，
因此 §3 的三阶段骨架在本流退化成「读状态 → 取回 → 逐封落库」）：

1. **读信箱状态**（事务外）：``read_mailbox_state`` 拿到当时的 ``UIDVALIDITY`` / ``UIDNEXT``。
   适配器把失败（连接 / TLS / 认证 / 超时）收进返回值，本层不需要为外部调用开宽捕获。
2. **初始化 / 重置游标**（短事务）：``ensure_cursor``——首见信箱写 ``last_uid = UIDNEXT - 1``，
   ``UIDVALIDITY`` 变化同样重置为「当时」的 ``UIDNEXT - 1``。两种情况都意味**这一轮一封都不拉**
   （区间 ``last_uid + 1 .. min(UIDNEXT - 1, last_uid + BATCH_SIZE)`` 为空），这正是 §3.4 登记的代价。
3. **增量取回**（事务外）：``fetch_messages(after_uid=last_uid, limit=BATCH_SIZE)``；失败（受控分类
   非空）时**不推进游标**，下一轮按同一区间重试。
4. **逐封独立短事务**：该封的分拣落库（写待审行 / 退信置失败 / 计数丢弃）与 ``advance_cursor``
   （把 ``last_uid`` 推到该封的 UID）在**同一个事务**里提交，然后才处理下一封。撞入站唯一键按
   ``duplicate`` 计数、游标照常推进——冲突不得把推进一起回滚，否则每一轮都重撞同一封、入站永久
   停摆（``AC-11`` / ``AC-39``）。

【日志口径】按 §3.4：有丢弃时记**一条** ``inbound_discarded``（十一类计数 + 总数）；连接失败或
单封处理异常时额外记一条受控错误事件（受控分类 / 异常类型 + 封数）。事件里只有受控事件名、计数与
受控分类——发件人、收件人、主题、正文、UID、原始头值一律不进日志（``AC-13`` / ``AC-28``、
``AGENTS.md`` §8 第 4 条）；正常 tick（连上了、拉回 0 封、无丢弃）不留记录是设计有意为之
（§3.4 已登记的残留盲区，本模块不自造一条 summary 事件去填它）。

【异常口径】本层唯一的捕获面是**单封事务**的 ``SQLAlchemyError``（具体类型）：DB 错误原文可能
回显行值（``CHECK`` 违例的 ``DETAIL``），不能进监督循环的 ``logger.exception``（``AGENTS.md``
§8 第 4 条）；吞掉该封、记一条受控错误事件后继续下一封——「单封异常只吞掉该封并计入受控原因，
绝不中止整批」（§3.4）。``docs/code-style.md`` §2.1 登记的 ``except Exception`` 例外现有三处（监督循环、
流层的单次审查调用、``/healthz`` 探测失败折叠），本模块不在其中，因此不用宽捕获；契约外的异常（本层代码自身的 bug）交给
监督循环留栈。**后果如实登记**：这一封的游标不推进（事务已回滚），但同一批里后续封的推进会越过
它——单封写库失败即等于这一封不再重试，`errored` 计数与那条错误事件是排查线索。

【收件人长度口径】§3.4 的入站行取值表要求「``From`` 超长时**截断后再入库**」，上限取配置项
``MAX_RECIPIENT_CHARS``（默认 320）：``recipient`` 列虽是 ``text``（无长度约束），但外部可控的头值
不得把整封写不进去。截断发生在写待审行之前，阈值只从配置读（本层不出现 320 这类字面量）。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from vs_email_forwarding.config import Settings, build_tls_context
from vs_email_forwarding.constants import (
    BODY_FORMAT_HTML,
    BODY_FORMAT_TEXT,
    DISCARD_REASON_DSN_TARGET_GONE,
    DISCARD_REASON_DSN_TARGET_NOT_SENT,
    DISCARD_REASON_DSN_WITHOUT_ID,
    DISCARD_REASON_DUPLICATE,
    DISCARD_REASON_MDN,
    DISCARD_REASON_NO_MATCH,
    DISCARD_REASON_NO_REPLY_FROM,
    DISCARD_REASON_UNPARSABLE,
    DISCARD_REASONS,
    ERROR_CLASS_INTERNAL,
    DiscardReason,
    ErrorClass,
)
from vs_email_forwarding.core.inbound import (
    InboundEmail,
    MimePart,
    is_delivery_status_notification,
    is_mdn,
    match_reference,
    parse_dsn,
    scan_references,
)
from vs_email_forwarding.db.repositories.cursors import advance_cursor, ensure_cursor
from vs_email_forwarding.db.repositories.inbound import insert_inbound_pending
from vs_email_forwarding.db.repositories.messages import (
    mark_failed_from_sent,
    outbound_targets_by_message_id,
)

# 会话工厂的类型别名与其余四条流共用一个定义（`flows/cleanup.py`），不在这里再造影子类型。
from vs_email_forwarding.flows.cleanup import SessionFactory
from vs_email_forwarding.providers.imap.base import (
    FetchedMessage,
    FetchedPart,
    ImapFetcher,
    MailboxState,
)
from vs_email_forwarding.providers.imap.imapclient_fetcher import ImapClientFetcher, ImapConfig

__all__ = ["FetchInboundResult", "fetch_inbound_tick"]

logger = structlog.get_logger(__name__)

#: 取时源：无参可调用、返回带时区的当前 UTC 时间。默认真时钟（:func:`_utc_now`）；
#: 测试注入步进时钟，即可让「逐封各取自己的写库时刻」成为确定性断言（`plan.md` §5.2）。
Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    """默认真时钟：带时区的 UTC 当前时刻（`AGENTS.md` §7 的时间口径）。"""
    return datetime.now(UTC)


# 受控取值一律从 constants 取具名常量，不在本模块重抄一遍（AGENTS.md §7）：丢弃原因按**名字**
# 取自具名常量（顺序即 design.md §3.4 的枚举顺序）；no_reference / malformed_reference 与
# no_match 由 core/inbound 的判定函数给出，本模块只再取回它的返回值比较，不重造。
_NO_MATCH = DISCARD_REASON_NO_MATCH
_MDN = DISCARD_REASON_MDN
_DSN_WITHOUT_ID = DISCARD_REASON_DSN_WITHOUT_ID
_DSN_TARGET_GONE = DISCARD_REASON_DSN_TARGET_GONE
_DSN_TARGET_NOT_SENT = DISCARD_REASON_DSN_TARGET_NOT_SENT
_DUPLICATE = DISCARD_REASON_DUPLICATE
_NO_REPLY_FROM = DISCARD_REASON_NO_REPLY_FROM
_UNPARSABLE = DISCARD_REASON_UNPARSABLE

# 服务自身内部故障（`ERROR_CLASS_INTERNAL`）：DSN 的 `Status` 取不出 4 / 5 子类时复用它
# （所有者裁决，`design.md` §3.3「退信到达」与 §2.2 的枚举表）——不写 NULL、不新造第四类。
_INTERNAL = ERROR_CLASS_INTERNAL

#: 中性结构的头名契约是小写（`providers/imap/base.py`）：判定输入按小写键取值。
_FROM_HEADER: Final = "from"
_SUBJECT_HEADER: Final = "subject"

#: 正文选取的两个 MIME content-type（§3.4 的入站行取值表）：优先 `text/plain`，没有则
#: `text/html` 并置 `html`。它们是 MIME 词汇、不是服务语义取值——与 `core/inbound.py` 的
#: `multipart/report` / `message/delivery-status` 同一口径，本地声明、不进 `constants`。
_TEXT_PLAIN: Final = "text/plain"
_TEXT_HTML: Final = "text/html"

# 正文格式取值取自 constants（受控枚举，写错会被 mypy 与库里的 CHECK 拦下）。
_TEXT_FORMAT = BODY_FORMAT_TEXT
_HTML_FORMAT = BODY_FORMAT_HTML


class _Outcome(StrEnum):
    """一封入站邮件的**成功**处置（内部词汇，不落库）。

    其余去向都是受控丢弃原因（:data:`~vs_email_forwarding.constants.DiscardReason`），
    由分拣函数直接返回，不再包一层枚举。
    """

    #: 关联回某封出站邮件的用户回复 → 写入待审表，等流④ 审查。
    STORED = "stored"
    #: 关联回某封出站邮件的退信 → 按 §3.3「退信到达」把该出站行置终态 `failed`。
    DSN_FAILED = "dsn_failed"


@dataclass(frozen=True, slots=True)
class FetchInboundResult:
    """一次 tick 的处置计数（只统计**已提交**的处置；进程内使用，不落库）。

    ``fetched`` 是本次取回的封数；``stored`` / ``dsn_failed`` / ``discarded`` 是逐封处置；
    ``errored`` 是单封事务失败被吞掉的封数（这类封不入十一类丢弃原因，另记一条受控错误事件）。
    """

    fetched: int = 0
    stored: int = 0
    dsn_failed: int = 0
    discarded: Mapping[DiscardReason, int] = field(default_factory=dict)
    errored: int = 0


async def fetch_inbound_tick(
    session_factory: SessionFactory,
    settings: Settings,
    *,
    clock: Clock | None = None,
) -> FetchInboundResult:
    """执行一次流③ tick：读信箱状态 → 增量取回一批 → 逐封分拣并同事务推进游标。

    ``clock`` 是**取时源**（缺省真时钟 :func:`_utc_now`），供测试注入步进时钟（``plan.md``
    §7.2：集成用例直接改列值造态，不靠 sleep）。时间分**两个层级**（``plan.md`` §5.2）：

    * **tick 级时刻**：本函数开头取一次，只用于游标初始化 / 推进与认领、退避的 ``t0``；
    * **行自身的写库时刻**：tick 循环里**每封各取一次**，用于该封自己的时间列——同一批内因此
      严格递增、批内先后即邮箱内顺序（``design.md`` §2.5「回复入库时刻」）。整 tick 取一次
      灌给全批会把同批行的时刻压成同一个值，让「按时刻排序」退化成按 ``id`` 决胜。

    连接失败不抛异常：适配器把失败收进返回值（``providers/imap/base.py`` 的接口契约），本层记一条
    受控错误事件后返回零计数；单封落库失败按 §3.4「只吞掉该封」处理（见模块 docstring 的
    【异常口径】）。
    """
    tick_now = clock() if clock is not None else _utc_now()
    fetcher = _build_fetcher(settings)
    state_result = await fetcher.read_mailbox_state(timeout_seconds=settings.imap_timeout_seconds)
    if state_result.state is None:
        # 受控错误事件：只有分类，没有对端回复原文（§3.4 的错误事件口径）。
        logger.warning(
            "inbound_fetch_failed",
            stage="mailbox_state",
            error_class=state_result.error_class,
        )
        return FetchInboundResult()
    last_uid = await _ensure_cursor(session_factory, state=state_result.state, now=tick_now)
    fetched = await fetcher.fetch_messages(
        after_uid=last_uid,
        limit=settings.batch_size,
        timeout_seconds=settings.imap_timeout_seconds,
    )
    if fetched.error_class is not None:
        # 失败时一封都不处理、游标不动：上一轮的区间留到下一轮重试（§3.4）。
        logger.warning("inbound_fetch_failed", stage="fetch", error_class=fetched.error_class)
        return FetchInboundResult()
    return await _process_all(
        session_factory,
        fetched.messages,
        tick_now=tick_now,
        clock=clock if clock is not None else _utc_now,
        max_recipient_chars=settings.max_recipient_chars,
    )


async def _ensure_cursor(
    session_factory: SessionFactory, *, state: MailboxState, now: datetime
) -> int:
    """初始化 / 重置游标，返回本轮的拉取起点 ``last_uid``（短事务）。

    必须**先读状态、再决定拉什么**（§3.4）：``ensure_cursor`` 在首见信箱与 ``UIDVALIDITY``
    变化两种情况下把 ``last_uid`` 写成「当时」的 ``UIDNEXT - 1``，于是本轮区间为空、一封不拉。
    """
    async with session_factory() as session:
        _, last_uid = await ensure_cursor(
            session,
            state.mailbox,
            uidvalidity=state.uidvalidity,
            uidnext=state.uidnext,
            now=now,
        )
        await session.commit()
    return last_uid


async def _process_all(
    session_factory: SessionFactory,
    messages: Sequence[FetchedMessage],
    *,
    tick_now: datetime,
    clock: Clock,
    max_recipient_chars: int,
) -> FetchInboundResult:
    """逐封独立短事务：分拣落库 + 同事务推进游标；单封失败只吞掉该封。

    每封一个事务是 §3.4 的硬口径：该封的分拣（写待审行 / 置失败 / 计数丢弃）与「把 ``last_uid``
    推到该封的 UID」一起提交。撞唯一键的落库被仓储的 ``on_conflict_do_nothing`` 按幂等吞掉后，
    同一事务里的推进照常提交——这正是「冲突不得把游标推进一起回滚」的落点（``AC-11`` / ``AC-39``）。

    ``FetchedMessage.mailbox`` / ``uidvalidity`` / ``uid`` 就是该封的入站幂等键（§2.2 的三列唯一
    索引），逐封自带：即使取回期间信箱轮换，落库用的仍是这一封自己的三要素。

    ``tick_now`` 只用于游标推进；每封自己的时间列取**进入循环时的那次 ``clock()``**（同一批内
    逐封取时，先后即邮箱内顺序，`design.md` §2.5）。
    """
    counts: Counter[DiscardReason] = Counter()
    stored = 0
    dsn_failed = 0
    errored = 0
    for message in messages:
        # 该封自己的写库时刻：同一批内每封各取一次，批内先后即邮箱内顺序（`design.md` §2.5）。
        message_now = clock()
        try:
            async with session_factory() as session:
                outcome = await _sort_and_land(
                    session,
                    message,
                    now=message_now,
                    max_recipient_chars=max_recipient_chars,
                )
                await advance_cursor(
                    session,
                    message.mailbox,
                    uidvalidity=message.uidvalidity,
                    uid=message.uid,
                    now=tick_now,
                )
                # 「处理完再推」：推进与分拣同事务提交，中途不再开第二个事务（§3.4）。
                await session.commit()
        except SQLAlchemyError as exc:
            # 受控错误事件 = 异常类型 + 计数，不含任何可回溯字段（§3.4 的日志口径；与
            # review_outbound / send_email 的结果事务失败事件同形：那边记 `exc_type` + `claimed`，
            # 这里记 `exc_type` + 本批取回封数）。一次失败一条，出问题时能看出是哪一批。
            logger.warning(
                "inbound_message_failed",
                exc_type=type(exc).__name__,
                fetched=len(messages),
            )
            errored += 1
            continue
        if outcome is _Outcome.STORED:
            stored += 1
        elif outcome is _Outcome.DSN_FAILED:
            dsn_failed += 1
        else:
            counts[outcome] += 1
    if counts:
        _log_discards(counts)
    return FetchInboundResult(
        fetched=len(messages),
        stored=stored,
        dsn_failed=dsn_failed,
        # 顺序固定为 constants 的枚举顺序，只列非零项（事件里则十一类齐全，见 _log_discards）。
        discarded={reason: counts[reason] for reason in DISCARD_REASONS if counts[reason] > 0},
        errored=errored,
    )


async def _sort_and_land(
    session: AsyncSession,
    message: FetchedMessage,
    *,
    now: datetime,
    max_recipient_chars: int,
) -> _Outcome | DiscardReason:
    """分拣一封并落库，返回成功处置或受控丢弃原因（不提交，事务由调用方划界）。

    四类去向按 §3.4 的表判定：**退信**（结构判定）→ **MDN**（结构判定）→ **关联回出站邮件的
    回复** → **其余**（丢弃）。判定全部来自 ``core/inbound.py`` 的纯逻辑，本层只做编排与落库；
    ``providers/imap`` 的中性结构在这里转成 ``core`` 的判定输入（边界转换在 ``flows/``，见
    ``providers/imap/base.py`` 的模块说明——两侧各自声明契约，``providers/`` 不 import ``core/``）。
    """
    email = _to_inbound_email(message)
    if is_delivery_status_notification(email):
        return await _apply_dsn(session, email, now=now)
    if is_mdn(email):
        return _MDN
    return await _land_reply(session, message, now=now, max_recipient_chars=max_recipient_chars)


async def _apply_dsn(
    session: AsyncSession, email: InboundEmail, *, now: datetime
) -> _Outcome | DiscardReason:
    """退信支：把 DSN 落到它指向的出站行（§3.3「退信到达」一行）。

    四种结局：取不出原邮件标识 → ``dsn_without_id``；标识在本表查不到（已被保留期清理）→
    ``dsn_target_gone``；查到但 ``sent_at`` 为空（尚未发出）→ ``dsn_target_not_sent``，
    **不置状态、不打断重试**；``sent_at`` 非空才置 ``failed``。分类按 §3.3：``5.x.x`` →
    ``permanent``、``4.x.x`` → ``transient``、**取不出时复用 ``internal``**（所有者裁决）
    ——不写 NULL、不新造第四类。``last_smtp_code`` 留空、``sent_at`` 保留、尝试列不动，都由
    仓储的条件 UPDATE 承担；重复投递同一封退信时该 UPDATE 写 0 行、按幂等忽略。
    """
    detail = parse_dsn(email)
    if detail.original_message_id is None:
        return _DSN_WITHOUT_ID
    targets = await outbound_targets_by_message_id(session, [detail.original_message_id])
    target = targets.get(detail.original_message_id)
    if target is None:
        return _DSN_TARGET_GONE
    target_id, sent_at = target
    if sent_at is None:
        return _DSN_TARGET_NOT_SENT
    error_class: ErrorClass = detail.status_class if detail.status_class is not None else _INTERNAL
    await mark_failed_from_sent(session, target_id, error_class=error_class, now=now)
    return _Outcome.DSN_FAILED


async def _land_reply(
    session: AsyncSession,
    message: FetchedMessage,
    *,
    now: datetime,
    max_recipient_chars: int,
) -> _Outcome | DiscardReason:
    """回复支：解析引用 → 关联出站行 → 取 `From` 与正文 → 幂等写待审行。

    引用解析与匹配是 ``core/inbound`` 的纯判定：``no_reference`` / ``malformed_reference`` 由
    ``scan_references`` 给出，``no_match``（引用存在但匹配不到本服务的出站邮件）由
    ``match_reference`` 给出。命中后取该出站行的主键作 ``parent_id``（§3.4：把标识解析成本表
    出站行的主键再写入回复行）。其余四种丢弃：``From`` 缺失或空白 → ``no_reply_from``
    （``recipient`` 是 NOT NULL，没有可入库的收件人）；两种文本部件都没有 → ``unparsable``；
    撞入站唯一键（这一封已经处理过）→ ``duplicate``——由仓储 ``on_conflict_do_nothing`` 的返回值
    判定，本层不靠异常文本匹配（``plan.md`` §2.9）。

    十一类丢弃原因里 ``reply_target_gone`` **不在本流产生**：它是流④ 写 ``messages`` 回复行时
    ``parent_id`` 外键违例的口径（§3.5），而本流写的是 ``pending_messages``——那张表上的
    ``parent_id`` 没有外键约束（``design.md`` §2.1），父行稍后被清理不会在这里挡住落库。
    """
    references = scan_references(message.headers)
    if isinstance(references, str):
        return references
    targets = await outbound_targets_by_message_id(session, list(references))
    matched = match_reference(references, set(targets))
    if matched == _NO_MATCH:
        return _NO_MATCH
    recipient = _reply_recipient(message.headers)
    if recipient is None:
        return _NO_REPLY_FROM
    # §3.4 的取值表：外部可控的头值不得把整封写不进去——入库前按配置上限截断（本层无字面量）。
    recipient = recipient[:max_recipient_chars]
    body = _select_body(message.parts)
    if body is None:
        return _UNPARSABLE
    text, body_format = body
    inserted = await insert_inbound_pending(
        session,
        recipient=recipient,
        # 主题缺失时存空串（§3.4 的取值表）；不做解码或折叠——中性结构交的就是原值。
        subject=message.headers.get(_SUBJECT_HEADER, ""),
        body=text,
        body_format=body_format,
        parent_id=targets[matched][0],
        mailbox=message.mailbox,
        uidvalidity=message.uidvalidity,
        uid=message.uid,
        now=now,
    )
    if not inserted:
        return _DUPLICATE
    return _Outcome.STORED


def _to_inbound_email(message: FetchedMessage) -> InboundEmail:
    """把 ``providers/imap`` 的中性结构转成 ``core/inbound`` 的判定输入（边界转换）。

    两侧各自声明同一份契约（架构上 ``providers/`` 不得 import ``core/``，见
    ``docs/architecture.md`` §2），字段逐项对应、只搬判定需要的部分：``FetchedPart.payload``
    是正文选取用的负载，判定输入不需要它（正文由本模块的 :func:`_select_body` 取）。
    """
    return InboundEmail(
        content_type=message.content_type,
        headers=message.headers,
        parts=tuple(
            MimePart(
                content_type=part.content_type,
                dsn_status=part.dsn_status,
                embedded_message_id=part.embedded_message_id,
            )
            for part in message.parts
        ),
    )


def _reply_recipient(headers: Mapping[str, str]) -> str | None:
    """取回复的 `From` 作入站行的 ``recipient``；缺失或空白给 ``None``。

    §3.4 的取值表要求「回复的 ``From``（回复者地址，外部可控、**原样入库**）」：本层不解析、
    不抽取地址、也不净化——头值原样取出（只按配置的 ``max_recipient_chars`` 截断，截断在
    :func:`_land_reply` 里做）。缺失或只有空白字符时没有可入库的收件人，调用方按
    ``no_reply_from`` 计数丢弃。
    """
    raw = headers.get(_FROM_HEADER)
    if raw is None or not raw.strip():
        return None
    return raw


def _select_body(parts: Sequence[FetchedPart]) -> tuple[str, str] | None:
    """按 §3.4 的取值表选正文：优先 ``text/plain``，没有则 ``text/html`` 并置 ``html``。

    返回 ``(正文, body_format)``；两种文本部件都没有（含解析不出任何部件的封）给 ``None``，
    调用方按 ``unparsable`` 计数丢弃。``payload`` 为空串是合法正文（空正文也是正文），只有
    ``None``（非文本部件）不算命中。
    """
    for content_type, body_format in ((_TEXT_PLAIN, _TEXT_FORMAT), (_TEXT_HTML, _HTML_FORMAT)):
        for part in parts:
            if part.content_type == content_type and part.payload is not None:
                return part.payload, body_format
    return None


def _log_discards(counts: Mapping[DiscardReason, int]) -> None:
    """记一条 ``inbound_discarded``：十一类计数 + 总数（§3.4 的日志口径）。

    字段是**全部十一类**（本轮没出现的记 0）：事件形状稳定，采集端不必区分「这一轮没有这类」
    与「字段缺失」；值只有计数，不含发件人 / 收件人 / 主题 / 正文 / UID / 原始头值
    （``AC-13`` / ``AC-28``、``AGENTS.md`` §8 第 4 条）。

    「没出现的记 0」靠的是 :meth:`~collections.abc.Mapping.get` 而不是下标：入参类型是
    ``Mapping``，契约里没有 ``Counter.__missing__``——用 ``counts[reason]`` 取十一类时，传进来的
    若是普通 Mapping（缺键即 ``KeyError``），这条承诺就变成一次抛异常。
    """
    logger.info(
        "inbound_discarded",
        **{reason: counts.get(reason, 0) for reason in DISCARD_REASONS},
        total=sum(counts.values()),
    )


def _build_fetcher(settings: Settings) -> ImapFetcher:
    """按配置装配入站拉取适配器（``providers/imap/base.py`` 的接口实现之一）。

    配置一律经 ``config.get_settings()`` 读好再注入（``design.md`` §4.5：配置唯一入口是
    ``config.py``），本层不读环境变量；口令只进构造函数，绝不出现在返回值、日志或异常里
    （``NFR-5``）。信箱**文件夹**取 ``IMAP_MAILBOX``——它就是入站行的 ``mailbox`` 列取值，
    别与信箱地址 ``IMAP_ADDRESS`` 混同（§4.4 特意拆成两个配置项）。返回类型写接口协议：
    调用面只依赖 ``ImapFetcher`` 的两个受控结果方法，装配点是阶段 5 集成夹具的替换处
    （``plan.md`` §7.2 的 ``imap_fake``）。
    """
    return ImapClientFetcher(
        ImapConfig(
            host=settings.imap_host,
            port=settings.imap_port,
            username=settings.imap_username,
            password=settings.imap_password,
            mailbox=settings.imap_mailbox,
            tls_mode=settings.imap_tls_mode,
            ssl_context=(build_tls_context(settings) if settings.imap_tls_mode == "ssl" else None),
            timeout_seconds=settings.imap_timeout_seconds,
        )
    )
