#!/usr/bin/env python3
"""入站拉取适配器的接口与中性结构（`design.md` §3.4 的三层落点）。

`providers/imap/` 只把邮件**取回并解析成中性结构**（顶层 content-type、头字段、
MIME 部件、`mailbox` / `uidvalidity` / `uid` 三要素），**不做任何判定**：DSN
结构判定、MDN 判定、`In-Reply-To` / `References` 的解析与关联匹配全在
`core/inbound.py`；「取回 → 判定 → 落库 / 置失败 / 计数丢弃」与游标逐封同事务
推进的编排在 `flows/` 的入站步骤（`design.md` §3.4、`plan.md` §5.4）。分拣规则
因此可脱离真实 IMAP 单测，换 IMAP 客户端也不影响分拣（§3.4）。

本模块只定契约：数据类 + `Protocol`。`IMAPClient` 实现（`asyncio.to_thread`、
`UIDVALIDITY` / `UIDNEXT`、逐封取回、超时）落在 `imapclient_fetcher.py`
（`plan.md` §2.3）：原始报文用标准库 `email` 解析后填充下面的结构。本模块因此
不 import 任何 IMAP 客户端库，也不 import `db/`、不读环境变量——配置一律由调用
方读好、经构造函数注入（`design.md` §4.5：唯一配置入口是 `config.py`）。

字段口径与 `core/inbound.py` 的 `InboundEmail` / `MimePart` 逐项对齐（小写无参
content-type、小写头名、缺失头不入映射、部件级 `dsn_status` /
`embedded_message_id`）。两侧各自声明、由 `flows/` 在边界处转换：按
`docs/architecture.md` §2 的依赖方向，`providers/` 不得依赖 `core/`（同先例：
`core/retry.py` 与 `providers/smtp/smtplib_sender.py` 各自本地声明错误分类词表）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

__all__ = [
    "FetchResult",
    "FetchedMessage",
    "FetchedPart",
    "ImapErrorClass",
    "ImapFetcher",
    "MailboxState",
    "MailboxStateResult",
]


@dataclass(frozen=True, kw_only=True)
class FetchedPart:
    """一个 MIME 部件的中性事实（`design.md` §3.4 的「MIME 部件」）。

    只有解析结果、没有任何判定：「是不是退信」「该不该入库」都不在这里。
    """

    #: 部件 content-type，一律小写、参数已去除（如 ``text/plain``）——判定与
    #: 正文选取都按这个归一化口径精确比较（`core/inbound.MimePart` 同契约）。
    content_type: str

    #: 该部件的文本负载（已按部件声明的字符集解码）；非文本部件为 ``None``。
    #: 入站行的 `body` / `body_format` 由 `flows/` 从这里按 `design.md` §3.4
    #: 「优先取 `text/plain`，没有则取 `text/html`」选取——本层不选。
    payload: str | None = None

    #: `message/delivery-status` 部件的 `Status` 字段原值（如 ``5.1.1``）；其余
    #: 部件为 ``None``。**取原值、不分类**：5.x.x / 4.x.x 的分类是
    #: `core/inbound.classify_dsn_status` 的职责（`design.md` §3.4）。RFC 3464
    #: 把 `Status` 放在 per-recipient 字段块里（per-message 块只有 `Reporting-MTA`
    #: 一类），本层交的是逐块取到的**第一个** `Status`。
    dsn_status: str | None = None

    #: `message/rfc822` 部件内嵌原邮件的 `Message-ID`（尖括号原样保留）；其余
    #: 部件为 ``None``。能否关联回本服务的出站邮件由 `core/inbound.py` 判定。
    embedded_message_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class FetchedMessage:
    """一封取回的入站邮件：UID 三要素 + 中性解析结果（`design.md` §3.4）。

    三要素（`mailbox` / `uidvalidity` / `uid`）就是入站幂等键（`design.md` §2.2
    的唯一约束），每封自带、落库时直接构成该键。`uidvalidity` 正常与同一次
    `MailboxState` 相同；只有取回过程中恰好发生 `UIDVALIDITY` 轮换才会不同，
    正确性由库里的唯一约束兜底（§3.4）。
    """

    #: 被拉取的信箱**文件夹**（配置项 `IMAP_MAILBOX` 的取值，如 ``INBOX``）。
    #: 注意别与 `IMAP_ADDRESS`（信箱地址）混为一谈——`design.md` §4.4 特意把
    #: 它们拆成两个配置项，不再依赖「两个字段恰好相等」。
    mailbox: str

    #: 取回时该信箱的 `UIDVALIDITY`；它一变，游标必须重置（`design.md` §3.4）。
    uidvalidity: int

    #: 该封在信箱内的 `UID`（同一 `UIDVALIDITY` 内唯一、单调递增）。
    uid: int

    #: 顶层 content-type，一律小写、参数已去除（如 ``multipart/report``）。
    content_type: str

    #: 头字段：头名一律小写，缺失的头不在映射里；值原样保留（不折叠、不解码），
    #: 同名头重复出现时取最早一条——判定只读单值头（`core/inbound.scan_references`
    #: 读 `in-reply-to` / `references`）。
    headers: Mapping[str, str]

    #: MIME 部件清单；解析不出任何可用部件时为空元组（调用方按 `design.md`
    #: §3.4 计入 ``unparsable`` 丢弃）。
    parts: tuple[FetchedPart, ...]


@dataclass(frozen=True, kw_only=True)
class MailboxState:
    """一次 `SELECT` 之后的信箱事实（`design.md` §3.4 的游标规则）。

    游标行不存在时初始化为 ``uidnext - 1``、`UIDVALIDITY` 变化时同样重置为
    「当时」的 ``uidnext - 1``（不是 0 / 1）——「当时」就是这个快照的取值，
    所以调用方必须**先读状态、再决定拉什么**。本层不读库、不碰游标：游标落库
    属 `db/` 仓储层（`plan.md` §2.2）。
    """

    #: 被拉取的信箱文件夹（口径同 `FetchedMessage.mailbox`）。
    mailbox: str

    #: 该信箱当前的 `UIDVALIDITY`；与游标行不一致即按 §3.4 重置游标。
    uidvalidity: int

    #: 下一个将被分配的 UID（`UIDNEXT`）；游标初始化与重置取 ``uidnext - 1``。
    uidnext: int


#: 取回失败的受控分类（`design.md` §2.4：`permanent` = 对端永久拒绝、
#: `transient` = 对端临时故障与连接 / 超时）。词表在此（契约模块）收窄声明、不取
#: `constants.ErrorClass`：那一族还含 `internal`（服务自身内部故障），适配器永远
#: 不产出它，词表要窄到本层实际产出的取值——先例是
#: `providers/smtp/smtplib_sender.py` 的 `SmtpErrorClass` 与
#: `core/inbound.py` 的 `DsnStatusClass`（运行期无第二份取值来源）。
ImapErrorClass = Literal["transient", "permanent"]


@dataclass(frozen=True, kw_only=True)
class MailboxStateResult:
    """`read_mailbox_state` 的受控结果：要么有状态、要么有受控失败分类。

    两个字段恰有一个非空（唯一构造方是 `imapclient_fetcher.py`）：``state`` 非空
    表示 `SELECT` 读到了信箱事实，``error_class`` 非空表示这次调用失败。接口要求
    故障「让调用方看得见、且与「连上了、这轮没拉到新邮件」可分辨」，本类型就是
    那个承载（`design.md` §3.4 未为取回定义结果类型，承载形态随 `T-20` / `T-21`
    落在本模块，正式补条目属 `T-42`）。
    """

    #: `SELECT` 之后的信箱事实（`UIDVALIDITY` / `UIDNEXT`）；失败时为 ``None``。
    state: MailboxState | None = None

    #: 本次调用的受控失败分类；成功时为 ``None``。
    error_class: ImapErrorClass | None = None


@dataclass(frozen=True, kw_only=True)
class FetchResult:
    """`fetch_messages` 的受控结果：取回的封（按 UID 升序）+ 受控失败分类。

    ``error_class`` 为 ``None`` 表示这次调用**成功**——此时 ``messages`` 为空元组
    的含义是「连上了、这个区间没有新邮件」，与失败（``error_class`` 非空、此时
    ``messages`` 一律为空）不是同一件事（见接口 docstring）。失败时上一轮的封没有
    丢：调用方不推进游标，下一轮按同一区间重试。
    """

    #: 取回并解析好的封，按 UID 升序；失败时为空元组。
    messages: tuple[FetchedMessage, ...] = ()

    #: 本次调用的受控失败分类；成功时为 ``None``。
    error_class: ImapErrorClass | None = None


class ImapFetcher(Protocol):
    """入站拉取接口：先读信箱状态、再按 UID 增量取回并解析（`design.md` §3.4）。

    实现（`imapclient_fetcher.py`）负责 IMAP 协议交互与标准库 `email` 解析；
    本接口只约定「交出中性事实」，判定与编排都在别处（见模块 docstring）。

    **两个方法的返回都是受控结果类型**（:class:`MailboxStateResult` /
    :class:`FetchResult`）：失败收进返回值、不抛给调用方，故障与「连上了、这轮
    没拉到新邮件」因此可分辨（`docs/code-style.md` §2）。

    接口不做任何配置读取：信箱文件夹、主机与凭据由实现持有构造函数注入的配置
    （`design.md` §4.5）；只有**与调用规模有关**的批量上限由调用方传参（`NFR-2`：
    每批条数是配置项，不写死在代码里）。
    """

    async def read_mailbox_state(self, *, timeout_seconds: int | None = None) -> MailboxStateResult:
        """`SELECT` 一次被拉取的信箱，回报 `UIDVALIDITY` 与 `UIDNEXT`。

        调用方必须**先读状态、再决定拉什么**：游标行不存在、或 `UIDVALIDITY`
        与游标行不一致时，游标要初始化为 / 重置为「当时」的 ``uidnext - 1``
        （`design.md` §3.4），而初始化与重置意味着**这一轮一封都不拉**——先拉后
        判会把轮换前已在信箱里的邮件错误地拉回来（§3.4 已登记这条代价）。

        返回 :class:`MailboxStateResult`：读到时带 `MailboxState`，故障时带受控
        分类而不抛异常——故障与「读到了状态」因此可分辨（调用方对前者额外记一条
        受控错误事件）。

        ``timeout_seconds`` 是单次调用的墙钟上界（配置项 `IMAP_TIMEOUT_SECONDS`，
        `design.md` §7）；传 ``None`` 时用实现自己的默认上界（墙钟到点同样按
        ``transient`` 收进返回值的 ``error_class``）。
        """
        ...

    async def fetch_messages(
        self, *, after_uid: int, limit: int, timeout_seconds: int | None = None
    ) -> FetchResult:
        """取回 UID **严格大于** ``after_uid`` 的最多 ``limit`` 封，逐封解析成中性结构。

        ``after_uid`` 直接传游标当前的 `last_uid`、``limit`` 传配置的批量上限
        （`BATCH_SIZE`）：区间就是 ``last_uid + 1`` 到 ``min(UIDNEXT - 1,
        last_uid + BATCH_SIZE)``（`design.md` §3.4；`NFR-2`）。返回按 UID 升序——
        调用方靠它**逐封**「处理该封 + 把游标推到该封的 UID」同事务推进。

        返回值必须**逐 UID 齐全**：每一个被取回的 UID 都要有对应记录，含结构
        解析不出可用部件的封——漏掉一封会让它永远挡住游标，而 §3.4 的游标规则
        与 `AC-39` 要求游标不停摆。

        返回 :class:`FetchResult`：``error_class is None`` 表示这次调用成功，此时
        ``messages`` 为空元组的含义是「连上了、这个区间没有新邮件」；``error_class``
        非空表示失败（此时 ``messages`` 一律为空），调用方**不推进游标**、下一轮
        按同一区间重试。故障（连接、TLS、认证、超时）因此必须让调用方看得见，且与
        「连上了、这轮没拉到新邮件」可分辨（`design.md` §3.4 要求连接失败额外记一条
        受控错误事件，而正常 tick 本就不留记录，两者混同会让排查失去唯一线索），
        但**不得静默吞掉**、也**不得抛给调用方**（`docs/code-style.md` §2）。
        `design.md` §3.4 没有为取回定义结果类型，承载形态就是本结果类型，正式补
        条目属 `T-42`。错误信息只描述「发生了什么」（用 UID 引用邮件），不含地址、
        正文、原始 MIME（`AGENTS.md` §8 第 4 条）。
        """
        ...
