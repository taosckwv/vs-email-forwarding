#!/usr/bin/env python3
"""入站分拣的纯判定（`design.md` §3.4 三层落点的 `core/` 层）。

`providers/imap/` 只把邮件取回并解析成中性结构（顶层 content-type、头字段、
MIME 部件清单），不做任何判定；本模块在纯数据上判定：DSN 结构判定、MDN
判定、`In-Reply-To` / `References` 的解析与关联匹配。「取回 → 判定 →
落库 / 置失败 / 计数丢弃」的编排属 `flows/` 入站步骤，不在本模块。零 I/O、
零数据库、不读配置（`plan.md` §2.1：分拣规则可脱离真实 IMAP 单测）。

按 `docs/architecture.md` §2，`core/` 的项目内依赖只有 constants 这一例外，
`core/` 之间不得互相 import——因此 DSN 状态分类词表（permanent /
transient，`design.md` §2.4 与 `core/retry.py` 的返回码分类同词表）在本地
以 Literal 固定；丢弃标识符的运行期取值与 constants.DISCARD_REASONS 同源，
由 `tests/unit/test_inbound_sorting.py` 的词表断言钉住。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Set
from dataclasses import dataclass
from typing import Final, Literal

from vs_email_forwarding.constants import DiscardReason

__all__ = [
    "DsnDetail",
    "DsnStatusClass",
    "InboundEmail",
    "MimePart",
    "classify_dsn_status",
    "is_delivery_status_notification",
    "is_mdn",
    "match_reference",
    "parse_dsn",
    "scan_references",
]

#: DSN Status 字段的分类词表（`design.md` §3.4：5.x.x 永久、4.x.x 临时）。
#: 与 `core/retry.py` 的 `SmtpErrorClass` 同词表（`design.md` §2.4），但
#: core 间依架构不得互相 import，故本地固定；运行期无新造标识。
type DsnStatusClass = Literal["permanent", "transient"]

#: msg-id 的形状：RFC 5322 的 ``<local@domain>``，尖括号原样保留——与库中
#: `message_id` 列（服务自己生成的出站 Message-ID，`design.md` §3.1）按
#: 精确字符串匹配。
_MSG_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"<[^<>]+>")


@dataclass(frozen=True, kw_only=True)
class MimePart:
    """一个 MIME 部件的中性事实（`providers/imap/` 解析后填充）。"""

    #: 部件 content-type，一律小写、参数已去除（中性结构契约）。
    content_type: str

    #: `message/delivery-status` 部件的 `Status` 字段值（如 ``5.1.1``）；其余
    #: 部件为 ``None``。RFC 3464 的 `Status` 在 per-recipient 字段块里
    #: （per-message 块只有 `Reporting-MTA` 一类），中性结构交的是逐块取到的
    #: **第一个** `Status`。
    dsn_status: str | None = None

    #: `message/rfc822` 部件内嵌原邮件的 `Message-ID`；其余部件为 ``None``。
    embedded_message_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class InboundEmail:
    """一封入站邮件的中性判定输入（与 T-20 `providers/imap/base.py` 对齐）。"""

    #: 顶层 content-type，一律小写、参数已去除（如 ``multipart/report``）。
    content_type: str

    #: 头字段：头名一律小写，缺失的头不在映射里。
    headers: Mapping[str, str]

    #: MIME 部件清单（含判定所需的部件级字段）。
    parts: tuple[MimePart, ...]


@dataclass(frozen=True, kw_only=True)
class DsnDetail:
    """退信的判定产物（前提：结构判定已认它是 DSN）。"""

    #: 内嵌 `message/rfc822` 取出的原邮件 Message-ID；取不出为 ``None``——
    #: 调用方按 `dsn_without_id` 计丢弃（`design.md` §3.4 登记的已知缺失）。
    original_message_id: str | None

    #: Status 字段的分类；缺失或分不出 4 / 5 类为 ``None``。
    status_class: DsnStatusClass | None


def is_delivery_status_notification(email: InboundEmail) -> bool:
    """结构判定退信：`multipart/report` 容器 **且** 含 `message/delivery-status` 部分。

    只认结构、不靠主题或正文猜（`design.md` §3.4）：主题写着投递失败字样的
    普通邮件不是退信；缺 `message/delivery-status` 部分的 `multipart/report`
    也不是。content-type 参数（如 `report-type`）不进判定——设计只认
    「容器 + 部分」两个条件。
    """
    return email.content_type == "multipart/report" and any(
        part.content_type == "message/delivery-status" for part in email.parts
    )


def is_mdn(email: InboundEmail) -> bool:
    """结构判定阅读回执：含 `message/disposition-notification` 部件即剔除。

    MDN 绝不允许被当成用户回复入库（`design.md` §3.4 四类去向表）；判据
    只认该部件的存在，不看正文措辞。
    """
    return any(part.content_type == "message/disposition-notification" for part in email.parts)


def classify_dsn_status(status: str) -> DsnStatusClass | None:
    """把 DSN 的 `Status` 字段分类为 permanent / transient；分不出返回 ``None``。

    增强码首个数字定类（`design.md` §3.4）：5.x.x → permanent、4.x.x →
    transient；其余（成功类 2.x.x、无法识别的值）分不出类，调用方按「仍是
    退信但无分类」处理。
    """
    leading = status.strip().split(".", 1)[0]
    if leading == "5":
        return "permanent"
    if leading == "4":
        return "transient"
    return None


def parse_dsn(email: InboundEmail) -> DsnDetail:
    """从退信取原邮件标识与状态分类。

    前提：``is_delivery_status_notification(email)`` 为真（本函数不重复结构
    判定）。原邮件标识取内嵌 `message/rfc822` 部件的 Message-ID；状态分类取
    `message/delivery-status` 部件的 `Status` 字段。两者都取不到时为
    ``None``，计数丢弃由流层做——纯判定只给事实。
    """
    original_message_id: str | None = None
    status_class: DsnStatusClass | None = None
    for part in email.parts:
        if part.content_type == "message/rfc822" and part.embedded_message_id is not None:
            original_message_id = original_message_id or part.embedded_message_id
        if part.content_type == "message/delivery-status" and part.dsn_status is not None:
            status_class = status_class or classify_dsn_status(part.dsn_status)
    return DsnDetail(original_message_id=original_message_id, status_class=status_class)


def scan_references(headers: Mapping[str, str]) -> tuple[str, ...] | DiscardReason:
    """解析 `In-Reply-To` / `References`，给出 msg-id 列表或丢弃原因。

    头名契约：小写（中性结构的归一化口径，缺失的头不在映射里）。两个头都
    没有 → ``no_reference``；头存在但解析不出任何 msg-id（含空值头）→
    ``malformed_reference``——头在场却给不出标识，按畸形对待而不是当作
    「没有引用」（`design.md` §3.4 十一类标识符）。提取出的 msg-id 尖括号
    原样保留、`In-Reply-To` 在前（它指向直接父级）、`References` 在后并
    去重保序——命中多个已知出站时以靠前者为父行。
    """
    in_reply_to = headers.get("in-reply-to")
    references = headers.get("references")
    if in_reply_to is None and references is None:
        return "no_reference"
    message_ids: list[str] = []
    # 去重靠 set 的成员判断（`docs/code-style.md` §1.5）：`References` 可能有上千个
    # msg-id，对 list 做 ``in`` 是 O(n²)；顺序仍由 list 保证（先 `In-Reply-To`、
    # 后者在前），set 只回答「这个标识见过没有」。
    seen: set[str] = set()
    for raw in (in_reply_to, references):
        if raw is None:
            continue
        for token in _MSG_ID_PATTERN.findall(raw):
            if token in seen:
                continue
            seen.add(token)
            message_ids.append(token)
    if not message_ids:
        return "malformed_reference"
    return tuple(message_ids)


def match_reference(
    message_ids: tuple[str, ...], known_message_ids: Set[str]
) -> str | DiscardReason:
    """把解析出的 msg-id 与本服务已知的出站 Message-ID 集合匹配。

    返回命中的出站 Message-ID（调用方再换行成 `parent_id`）；一个都匹配
    不上 → ``no_match``（引用存在但匹配不到本服务的出站邮件，`design.md`
    §3.4）。「指向的行已被清理 / 尚未发出」（`dsn_target_gone` /
    `dsn_target_not_sent` / `reply_target_gone`）要看库内行状态，属流层
    细分，不在纯判定里。
    """
    for message_id in message_ids:
        if message_id in known_message_ids:
            return message_id
    return "no_match"
