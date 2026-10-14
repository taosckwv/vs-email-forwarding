#!/usr/bin/env python3
"""基于 `IMAPClient` 的入站拉取实现（`design.md` §3.4、`plan.md` §2.3）。

`IMAPClient` 是同步库，阻塞调用不得放进事件循环，因此每次调用都经
:func:`asyncio.to_thread` 执行（与 SMTP 侧同构，`design.md` §3.3），并且两层
上界都由注入的 `IMAP_TIMEOUT_SECONDS` 给出：`IMAPClient` 的套接字超时，以及
:func:`asyncio.wait_for` 的墙钟上界。两者都由调用方传入——本模块不读配置、不读
环境变量（`design.md` §4.5：唯一配置入口是 `config.py`），装配在阶段 4 的
`flows/`。

**增量口径**（`design.md` §3.4）：先 `SELECT` 读一次信箱状态，再决定拉什么。
游标行不存在时初始化为该信箱当时的 ``UIDNEXT - 1``、`UIDVALIDITY` 变化时同样
重置为「当时」的 ``UIDNEXT - 1``（不是 0 / 1）——本模块只交出「当前
`UIDVALIDITY` / `UIDNEXT` 与待取区间」这一增量信息，游标落库属 `db/` 仓储层
（`plan.md` §2.2）。逐封取回的上界是 ``min(UIDNEXT - 1, last_uid + limit)``：
``limit`` 由调用方注入（配置项 `BATCH_SIZE`），代码里不出现批量字面量（`NFR-2`）。

**失败只回传受控分类、绝不抛给调用方**：每次调用返回 `base.py` 的 `MailboxStateResult`
/ `FetchResult`（随接口契约一起落在那个模块），故障与「连上了、这轮没拉到新邮件」
因此可分辨、也不静默（`docs/code-style.md` §2）。这是 `T-19` 的判例（`t36-F1`）
在拉取侧的泛化：适配器把边界收全，越界或对端异常一律转成受控结果。`design.md`
§3.4 还**没有**为取回定义结果类型，正式补条目属 `T-42`，现阶段不动文档。

**只取回、不判定**：报文用标准库 `email` 解析成 `providers/imap/base.py` 的中性
结构（顶层 content-type、头字段、MIME 部件、`mailbox` / `uidvalidity` / `uid` 三
要素）；DSN 结构判定、MDN 判定、`References` / `In-Reply-To` 的解析与关联匹配全在
`core/inbound.py`，编排在 `flows/`（`design.md` §3.4 的三层落点）。**本模块不记
日志、不落自由文本**：异常信息、事件与返回值里都不出现地址、主题、正文、UID 或
原始 MIME（`AGENTS.md` §8 第 4 条、`NFR-10`），受控事件由 `flows/` 记。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from email import message_from_bytes
from email.message import Message

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError, LoginError

from vs_email_forwarding.providers.imap.base import (
    FetchedMessage,
    FetchedPart,
    FetchResult,
    ImapErrorClass,
    MailboxState,
    MailboxStateResult,
)

#: `FetchResult` / `ImapErrorClass` / `MailboxStateResult` 取自契约模块 `base.py`
#: （`T-20` 落地）：本模块只再导出它们，接口与实现共用的结果类型定义在那一处。
__all__ = [
    "FetchResult",
    "ImapClientFetcher",
    "ImapConfig",
    "ImapErrorClass",
    "MailboxStateResult",
]

#: 文本负载解码的兜底字符集：部件没声明字符集、或声明了 Python 不认识的字符集名
#: （现实里存在 ``unknown-8bit`` 这类取值）时用它再解一次，绝不因编码问题让整封
#: 取回失败。
_FALLBACK_CHARSET = "utf-8"

#: 取回报文的 FETCH 数据项：整个 RFC 822 报文（解析在 `_parse_message` 里做）。
_FETCH_DATA: tuple[bytes, ...] = (b"RFC822",)


@dataclass(frozen=True, kw_only=True)
class ImapConfig:
    """`IMAPClient` 实现所需的全部注入参数（`design.md` §4.5 的参数来源）。

    由调用方（阶段 4 的 `flows/`）从 `config.get_settings()` 读好再传入：本模块
    与 `providers/` 其它模块一样不碰配置层，也不读环境变量。
    """

    #: IMAP 主机与端口（`IMAP_HOST` / `IMAP_PORT`）。
    host: str
    port: int

    #: 登录名与口令（`IMAP_USERNAME` / `IMAP_PASSWORD`）。口令只用于登录，绝不出现在
    #: 返回结果、日志或异常里（`NFR-5`）。
    username: str
    password: str

    #: 被拉取的**信箱文件夹**（`IMAP_MAILBOX`，如 ``INBOX``）；它就是入站行的
    #: `mailbox` 列取值（`design.md` §2.1）。别与信箱地址 `IMAP_ADDRESS` 混同
    #: （§4.4 特意拆成两个配置项）。
    mailbox: str

    #: 是否用 STARTTLS 升级连接（`IMAP_STARTTLS`）。配置里只有这一个 TLS 开关，
    #: 本层就不发明第二个：连接一律以明文建立、`starttls()` 按需升级（与
    #: `providers/smtp/` 的 `starttls` 同构）。
    starttls: bool

    #: 单次调用的墙钟上界（秒，`IMAP_TIMEOUT_SECONDS`；方法的 ``timeout_seconds``
    #: 实参未给时用它）。
    timeout_seconds: int


class ImapClientFetcher:
    """用 `IMAPClient` 增量拉入站邮件，经 `asyncio.to_thread` 调用。

    构造参数就是配置数据类 `ImapConfig`——由调用方从配置读好注入（`design.md`
    §4.5）：

    ::

        ImapClientFetcher(ImapConfig(host=..., port=..., username=..., password=...,
                                     mailbox=..., starttls=..., timeout_seconds=...))

    本类不读环境变量、不 import `config`——「装配」发生在阶段 4 的 `flows/`。
    方法名与形参跟 `providers/imap/base.py` 的 `ImapFetcher` 逐项对齐（同一道
    接缝），返回类型就是那一侧的 `MailboxStateResult` / `FetchResult`（两者定义在
    契约模块 `base.py`）：`design.md` §3.4 没有为取回定义结果类型，而「适配器不抛
    异常、故障与 0 封可分辨」要求返回里带上受控分类（所有者裁决，泛化 `t36-F1`
    判例）。每次调用各自建立一次会话：接口就是「先读状态、再按区间取回」两步，
    跨调用复用连接会让超时上界与故障隔离都变复杂；代价是每个 tick 两条连接
    （如实登记）。
    """

    def __init__(self, config: ImapConfig) -> None:
        self._config = config

    async def read_mailbox_state(self, *, timeout_seconds: int | None = None) -> MailboxStateResult:
        """`SELECT` 一次被拉取的信箱，回报 `UIDVALIDITY` 与 `UIDNEXT`。

        ``timeout_seconds`` 缺省时用构造时注入的 `IMAP_TIMEOUT_SECONDS` 值。墙钟
        上界到点（`TimeoutError`）按临时故障计：调用方不推进游标，下一轮重读。
        """
        timeout = self._timeout(timeout_seconds)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_read_state_blocking, self._config, timeout), timeout
            )
        except TimeoutError as exc:
            # 墙钟上界到点：本次调用按临时故障计；线程本身无法取消，只能弃用它的结果。
            return MailboxStateResult(error_class=_failure_class(exc))

    async def fetch_messages(
        self, *, after_uid: int, limit: int, timeout_seconds: int | None = None
    ) -> FetchResult:
        """取回 UID 严格大于 ``after_uid`` 的最多 ``limit`` 封，逐封解析成中性结构。

        ``after_uid`` 传游标当前的 `last_uid`、``limit`` 传配置的批量上限
        （`BATCH_SIZE`）：区间就是 ``after_uid + 1`` 到 ``min(UIDNEXT - 1,
        after_uid + limit)``（`design.md` §3.4）。返回按 UID 升序，调用方靠它逐封
        「处理该封 + 把游标推到该封的 UID」同事务推进。
        """
        timeout = self._timeout(timeout_seconds)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    _fetch_blocking, self._config, after_uid=after_uid, limit=limit, timeout=timeout
                ),
                timeout,
            )
        except TimeoutError as exc:
            # 墙钟上界到点：本次调用按临时故障计；线程本身无法取消，只能弃用它的结果。
            return FetchResult(error_class=_failure_class(exc))

    def _timeout(self, timeout_seconds: int | None) -> int:
        """单次调用的墙钟上界：调用方未给时用注入的 `IMAP_TIMEOUT_SECONDS`。"""
        return timeout_seconds if timeout_seconds is not None else self._config.timeout_seconds


def _failure_class(cause: BaseException | None) -> ImapErrorClass:
    """把本层的失败来源翻译成受控分类——**唯一的映射点**（`design.md` §2.4）。

    映射只有两条：`LoginError` 是对端明确拒绝认证，同样的凭据重试不会成功，按
    ``permanent``；其余失败来源一律 ``transient``——协议层异常与连接 / TLS / 超时
    类故障（`IMAPClientError` / `OSError`），以及 ``None`` 这个「调用没拿到结果」
    的兜底（`SELECT` / `FETCH` 返回空）。**分类只依据来源类型，不看对端回复的原始
    文本**：它可能回显地址或正文（`AGENTS.md` §8 第 4 条）。

    捕获哪些异常仍由各调用点自己的 ``except`` 子句决定（`docs/code-style.md`
    §1.3：`try` 只包住会抛异常的那一次调用）；本函数只负责把已捕获的东西翻成
    分类，不改动边界。两个结果类型（信封）刻意不并：它们的字段不同，合并会造出
    「有 messages 又报错」这类自相矛盾的实例（`docs/code-style.md` §1.6）。
    """
    return "permanent" if isinstance(cause, LoginError) else "transient"


def _read_state_blocking(config: ImapConfig, timeout: int) -> MailboxStateResult:
    """同步读状态（由 `asyncio.to_thread` 在线程里执行）。

    ``try`` 只包住真正会抛异常的那一次 `SELECT`（`docs/code-style.md` §1.3）；
    捕获处一律经 :func:`_failure_class` 转成受控分类，不留自由文本、也不记日志
    （异常原文可能回显地址或正文，`AGENTS.md` §8 第 4 条）。
    """
    try:
        state = _select(config, timeout)
    except (LoginError, IMAPClientError, OSError) as exc:
        return MailboxStateResult(error_class=_failure_class(exc))
    if state is None:
        return MailboxStateResult(error_class=_failure_class(None))
    return MailboxStateResult(state=state)


def _fetch_blocking(config: ImapConfig, *, after_uid: int, limit: int, timeout: int) -> FetchResult:
    """同步取回（由 `asyncio.to_thread` 在线程里执行）；异常口径同 `_read_state_blocking`。"""
    try:
        fetched = _fetch(config, after_uid=after_uid, limit=limit, timeout=timeout)
    except (LoginError, IMAPClientError, OSError) as exc:
        return FetchResult(error_class=_failure_class(exc))
    if fetched is None:
        return FetchResult(error_class=_failure_class(None))
    uidvalidity, raw_by_uid = fetched
    return FetchResult(
        messages=_parse_all(config.mailbox, uidvalidity=uidvalidity, raw_by_uid=raw_by_uid)
    )


def _select(config: ImapConfig, timeout: int) -> MailboxState | None:
    """`SELECT` 一次信箱，读成中性事实；响应缺必需字段时给 ``None``。

    ``readonly=True``：入站只为取回报文，不碰信箱里的任何标志位。
    """
    with _session(config, timeout) as client:
        selected = client.select_folder(config.mailbox, readonly=True)
    uidvalidity = _required_int(selected, b"UIDVALIDITY")
    uidnext = _required_int(selected, b"UIDNEXT")
    if uidvalidity is None or uidnext is None:
        return None
    return MailboxState(mailbox=config.mailbox, uidvalidity=uidvalidity, uidnext=uidnext)


def _fetch(
    config: ImapConfig, *, after_uid: int, limit: int, timeout: int
) -> tuple[int, Mapping[int, Mapping[bytes, object]]] | None:
    """一次会话内 `SELECT` + 按区间 `FETCH`；给出 ``(UIDVALIDITY, {UID: 原始报文})``。

    `UIDVALIDITY` 与 `UIDNEXT` 读不出时给 ``None``：RFC 3501 要求 `SELECT` 必带
    这两个响应码，缺了就是对端协议层异常，按可重试的 transient 记、绝不抛给
    调用方（`t36-F1` 判例的泛化：适配器的边界是全量的）。
    """
    with _session(config, timeout) as client:
        selected = client.select_folder(config.mailbox, readonly=True)
        uidvalidity = _required_int(selected, b"UIDVALIDITY")
        uidnext = _required_int(selected, b"UIDNEXT")
        if uidvalidity is None or uidnext is None:
            return None
        raw_by_uid = client.fetch(
            _uid_interval(after_uid=after_uid, limit=limit, uidnext=uidnext), _FETCH_DATA
        )
    return uidvalidity, raw_by_uid


@contextmanager
def _session(config: ImapConfig, timeout: int) -> Iterator[IMAPClient]:
    """打开一次会话（连接 + 可选的 STARTTLS + 登录），退出路径一律收尾。

    用 `with` 管连接的释放（`docs/code-style.md` §1.4）：取回成功、对端拒绝、
    I/O 故障三条路径都必须在退出时收掉连接。`ssl=False` + 显式 `starttls()` 是
    `IMAP_STARTTLS` 的字面口径（与 `providers/smtp/` 的 `starttls` 同构）。
    """
    with IMAPClient(config.host, config.port, ssl=False, timeout=timeout) as client:
        if config.starttls:
            client.starttls()
        client.login(config.username, config.password)
        yield client


def _required_int(selected: Mapping[bytes, object], key: bytes) -> int | None:
    """从 `SELECT` 响应里取一个必须存在的整型响应码；读不出给 ``None``。

    `IMAPClient` 的包目录里没有 `py.typed`（`docs/research/03-dependencies-and-
    toolchain.md`），库返回值在本模块按本地契约标注——这一层就是那道边界。
    """
    value = selected.get(key)
    return value if isinstance(value, int) else None


def _uid_interval(*, after_uid: int, limit: int, uidnext: int) -> range:
    """本轮要取的 UID 区间：``last_uid + 1`` 到 ``min(UIDNEXT - 1, last_uid + limit)``。

    `design.md` §3.4 的两条口径都在这里：逐封增量（严格大于游标）与每 tick 封数上限；
    上界再被信箱事实截断（``UIDNEXT - 1`` 是信箱里最后一个 UID）。``limit`` 由调用方
    注入——代码里不出现批量字面量（`NFR-2`）。区间为空时是空 `range`，`FETCH` 一次
    也不发。
    """
    upper = min(uidnext - 1, after_uid + limit)
    return range(after_uid + 1, upper + 1)


def _parse_all(
    mailbox: str, *, uidvalidity: int, raw_by_uid: Mapping[int, Mapping[bytes, object]]
) -> tuple[FetchedMessage, ...]:
    """把取回的原始报文逐封解析成中性结构，按 UID 升序返回。

    **逐 UID 齐全**（`base.py` 的契约）：`FETCH` 回来的每个 UID 都产出一条记录——
    漏掉一封会让它挡住游标（`design.md` §3.4 与 `AC-39`）。对端理论上必回字节，
    非字节按空报文（``b""``）解析：标准库对空报文的解析产物是 **``text/plain``
    根部件、``payload`` 为空串**（实测），因此这条记录照旧走**常规分拣**——由
    `flows/` 按结构 / 引用判定决定去向，**不是**按 ``unparsable`` 丢弃；本层只
    保证不少一封。
    """
    messages: list[FetchedMessage] = []
    for uid in sorted(raw_by_uid):
        raw = raw_by_uid[uid].get(b"RFC822")
        messages.append(
            _parse_message(
                raw if isinstance(raw, bytes) else b"",
                mailbox=mailbox,
                uidvalidity=uidvalidity,
                uid=uid,
            )
        )
    return tuple(messages)


def _parse_message(raw: bytes, *, mailbox: str, uidvalidity: int, uid: int) -> FetchedMessage:
    """把一封原始报文解析成中性结构（**不做任何判定**）。

    判定（DSN / MDN / 引用关联）全在 `core/inbound.py`（`design.md` §3.4 的三层
    落点），本函数只把标准库 `email` 的解析结果翻译成 `base.py` 的字段。用标准库
    的兼容策略（`email.message_from_bytes` 的默认策略）读头字段：它**不折叠、不
    解码 RFC 2047**，与 `base.py` 的头字段口径一致。
    """
    message = message_from_bytes(raw)
    return FetchedMessage(
        mailbox=mailbox,
        uidvalidity=uidvalidity,
        uid=uid,
        content_type=_content_type(message),
        headers=_headers(message),
        parts=_parts(message),
    )


def _content_type(message: Message) -> str:
    """content-type：小写、参数已去除（`Message.get_content_type` 的口径）。

    缺 `Content-Type` 头或头值畸形时，标准库按 RFC 2045 的默认值给 ``text/plain``。
    """
    return str(message.get_content_type())


def _headers(message: Message) -> Mapping[str, str]:
    """头名小写、同名重复取最早一条、缺失的头不入映射（`base.py` 的字段口径）。

    值原样保留：非 ASCII 头值在兼容策略下是 `email.header.Header` 对象，`str()`
    化后仍是「RFC 2047 未解码、折行未展开」的原样视图——契约要的就是原样，判定侧
    （`core/inbound.scan_references`）自己在原值上找 msg-id。
    """
    headers: dict[str, str] = {}
    for name, value in message.items():
        key = name.lower()
        if key not in headers:
            headers[key] = str(value)
    return headers


def _parts(message: Message) -> tuple[FetchedPart, ...]:
    """展开 MIME 部件（深度优先、含容器本身）；``message/*`` 部件是叶子、不下钻。

    不下钻的原因：``message/rfc822`` 里是**原邮件**、``message/delivery-status``
    里是状态字段块，都不是本次入站的部件——标准库的 ``Message.walk`` 会下钻，那就
    把原邮件的正文混成本次回复的正文（`base.py` 的两层结构：容器 + 部分）。
    """
    parts: list[FetchedPart] = []
    pending: list[Message] = [message]
    while pending:
        current = pending.pop()
        content_type = _content_type(current)
        parts.append(_part(current, content_type=content_type))
        if not content_type.startswith("message/"):
            # 子部件按深度优先的次序入栈：LIFO 取用 + 反序入栈恰好得到先序。
            pending.extend(reversed(_subparts(current)))
    return tuple(parts)


def _subparts(message: Message) -> tuple[Message, ...]:
    """取子部件（或 `message/delivery-status` 的字段块）；负载不是报文列表时为空元组。"""
    payload = message.get_payload()
    if not isinstance(payload, list):
        return ()
    return tuple(part for part in payload if isinstance(part, Message))


def _part(message: Message, *, content_type: str) -> FetchedPart:
    """把一个 MIME 部件翻译成中性事实（只有事实，没有判定）。"""
    return FetchedPart(
        content_type=content_type,
        payload=_text_payload(message) if content_type.startswith("text/") else None,
        dsn_status=_dsn_status(message) if content_type == "message/delivery-status" else None,
        embedded_message_id=(
            _embedded_message_id(message) if content_type == "message/rfc822" else None
        ),
    )


def _text_payload(message: Message) -> str:
    """按部件声明的字符集解码文本负载（`base.py` 的 `payload` 口径）。

    解码是尽力而为、绝不抛：字符集名不认识时回退到兜底字符集，解码错误用
    ``errors="replace"`` 记成替换符——正文要进库，替换符让「有内容但编码坏了」
    留下痕迹，而代理字符（``surrogateescape`` 的产物）写不进库。不留日志：本层
    不记录任何可回溯字段（`AGENTS.md` §8 第 4 条），受控事件由 `flows/` 记。
    """
    raw = message.get_payload(decode=True)
    body = raw if isinstance(raw, bytes) else b""
    charset = message.get_content_charset() or _FALLBACK_CHARSET
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode(_FALLBACK_CHARSET, errors="replace")


def _dsn_status(message: Message) -> str | None:
    """取 `message/delivery-status` 里第一个 ``Status`` 字段的原值（如 ``5.1.1``）。

    RFC 3464 把该部件的负载切成若干字段块：首块是 per-message 字段，其后每块对应
    一个收件人，而 ``Status`` 属 per-recipient 字段——因此逐块找第一个出现的取值。
    **只取原值、不分类**：``5.x.x`` / ``4.x.x`` 的分类是
    `core/inbound.classify_dsn_status` 的职责（`design.md` §3.4）。
    """
    for block in _subparts(message):
        status = block.get("Status")
        if status is not None:
            return str(status)
    return None


def _embedded_message_id(message: Message) -> str | None:
    """取 `message/rfc822` 内嵌原邮件的 `Message-ID`（尖括号原样保留）。

    取不出（内嵌报文缺失或没有该头）即 ``None``：调用方按 `dsn_without_id` 计数
    丢弃（`design.md` §3.4 登记的已知缺失）。
    """
    subparts = _subparts(message)
    if not subparts:
        return None
    message_id = subparts[0].get("Message-ID")
    return None if message_id is None else str(message_id)
