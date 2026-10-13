#!/usr/bin/env python3
"""入站纯判定（`core/inbound.py`）的单测。

覆盖：DSN 的结构判定（只认 `multipart/report` 容器 + `message/delivery-status`
部分，不靠主题 / 正文猜）、DSN 详情解析（内嵌 `message/rfc822` 的原邮件标识、
Status 字段 5.x.x / 4.x.x 分类、**Status 字段缺失时的无分类路径**）、MDN 判定
（`message/disposition-notification`）、
`In-Reply-To` / `References` 的解析（msg-id 列表、`no_reference` /
`malformed_reference`）与关联匹配（命中 / `no_match`），以及本模块产出的丢弃
标识符确实取自 `constants.DISCARD_REASONS` 词表（`AC-13` 的判定基础）。

期望值来源（`docs/testing.md` R2，全部为规格推导字面量）：

* DSN / MDN 判据与丢弃原因：`design.md` §3.4（四类去向表、十一类标识符、
  「退信识别按结构判断，不靠主题或正文猜」）；
* Status 分类：`design.md` §3.4「`Status: 5.x.x`（永久）与 `4.x.x`（临时）」；
* msg-id 形状：RFC 5322 的 ``<local@domain>``，尖括号原样保留，与库中
  `message_id` 列（形如 ``<{uuid}@{域}>``，design.md §3.1）精确匹配；
* 丢弃标识符词表：`vs_email_forwarding.constants.DISCARD_REASONS`。
"""

from __future__ import annotations

import pytest

from vs_email_forwarding import constants
from vs_email_forwarding.core.inbound import (
    InboundEmail,
    MimePart,
    classify_dsn_status,
    is_delivery_status_notification,
    is_mdn,
    match_reference,
    parse_dsn,
    scan_references,
)

#: 服务生成的出站 Message-ID（design.md §3.1 的形状），全 1 UUID 便于逐字符核对。
_OUTBOUND_MID = "<11111111-1111-1111-1111-111111111111@example.com>"


def test_dsn_detected_by_structure() -> None:
    # design.md §3.4：multipart/report 容器 + message/delivery-status 部分，
    # 两个条件齐了才是退信。
    email = InboundEmail(
        content_type="multipart/report",
        headers={"subject": "Undelivered Mail Returned to Sender"},
        parts=(
            MimePart(content_type="message/delivery-status", dsn_status="5.1.1"),
            MimePart(content_type="text/plain"),
            MimePart(content_type="message/rfc822", embedded_message_id=_OUTBOUND_MID),
        ),
    )
    assert is_delivery_status_notification(email) is True


def test_dsn_rejected_without_delivery_status_part() -> None:
    # 结构判定的反例一侧：只有 multipart/report 容器、没有
    # message/delivery-status 部分 → 不是退信。
    email = InboundEmail(
        content_type="multipart/report",
        headers={},
        parts=(MimePart(content_type="text/plain"),),
    )
    assert is_delivery_status_notification(email) is False


def test_dsn_rejected_without_report_container() -> None:
    # 结构判定的反例另一侧：有 message/delivery-status 部件但顶层不是
    # multipart/report 容器 → 不是退信。
    email = InboundEmail(
        content_type="multipart/mixed",
        headers={},
        parts=(MimePart(content_type="message/delivery-status", dsn_status="5.1.1"),),
    )
    assert is_delivery_status_notification(email) is False


def test_dsn_not_guessed_from_subject() -> None:
    # design.md §3.4：退信识别按结构判断，不靠主题或正文猜——主题写着
    # 投递失败字样的普通邮件不是退信。
    email = InboundEmail(
        content_type="text/plain",
        headers={"subject": "Delivery Status Notification (Failure)"},
        parts=(MimePart(content_type="text/plain"),),
    )
    assert is_delivery_status_notification(email) is False


@pytest.mark.parametrize(
    ("dsn_status", "expected_class"), [("5.1.1", "permanent"), ("4.2.2", "transient")]
)
def test_parse_dsn_returns_original_id_and_status_class(
    dsn_status: str, expected_class: str
) -> None:
    # design.md §3.4：原邮件标识取内嵌 message/rfc822 部件的 Message-ID；
    # Status 首位 5 → permanent、4 → transient。
    email = InboundEmail(
        content_type="multipart/report",
        headers={},
        parts=(
            MimePart(content_type="message/delivery-status", dsn_status=dsn_status),
            MimePart(content_type="text/plain"),
            MimePart(content_type="message/rfc822", embedded_message_id=_OUTBOUND_MID),
        ),
    )
    detail = parse_dsn(email)
    assert detail.original_message_id == _OUTBOUND_MID
    assert detail.status_class == expected_class


def test_parse_dsn_without_embedded_original_gives_no_id() -> None:
    # design.md §3.4：取不出原邮件标识的退信按 dsn_without_id 丢弃（登记过的
    # 已知缺失）——纯判定给出 None，计数由流层做。
    email = InboundEmail(
        content_type="multipart/report",
        headers={},
        parts=(
            MimePart(content_type="message/delivery-status", dsn_status="5.0.0"),
            MimePart(content_type="text/plain"),
        ),
    )
    assert parse_dsn(email).original_message_id is None


def test_parse_dsn_without_status_field_gives_no_status_class() -> None:
    # design.md §3.4：部件的 Status 字段取不到（这里 message/delivery-status
    # 部件在场、字段缺失）→ 分不出类，纯判定给 None，调用方按「仍是退信但无
    # 分类」处理。与上面 original_message_id 缺失同型：**缺失不等于空串**——
    # 若实现把缺失当空串初始化，空串会走分类路径，语义就从「无分类」漂成
    # 「有字段但分不出」，两者在调用方是两回事。
    email = InboundEmail(
        content_type="multipart/report",
        headers={},
        parts=(
            MimePart(content_type="message/delivery-status"),
            MimePart(content_type="text/plain"),
        ),
    )
    assert parse_dsn(email).status_class is None


@pytest.mark.parametrize(
    ("dsn_status", "expected_class"),
    [("5.1.1", "permanent"), ("4.4.1", "transient"), ("2.0.0", None), ("not-a-code", None)],
)
def test_classify_dsn_status_by_leading_digit(dsn_status: str, expected_class: str | None) -> None:
    # design.md §3.4 只定义 5.x.x / 4.x.x 两类；成功类 2.x.x 与无法识别的值
    # 分不出类（None），由调用方按「仍是退信但无分类」处理。
    assert classify_dsn_status(dsn_status) == expected_class


def test_mdn_detected_by_disposition_part() -> None:
    # design.md §3.4：MDN（阅读回执）剔除、绝不允许被当成用户回复——判据是
    # message/disposition-notification 部件。
    email = InboundEmail(
        content_type="multipart/report",
        headers={},
        parts=(
            MimePart(content_type="message/disposition-notification"),
            MimePart(content_type="text/plain"),
        ),
    )
    assert is_mdn(email) is True


@pytest.mark.parametrize(
    "email",
    [
        InboundEmail(
            content_type="multipart/report",
            headers={},
            parts=(
                MimePart(content_type="message/delivery-status", dsn_status="5.1.1"),
                MimePart(content_type="message/rfc822", embedded_message_id=_OUTBOUND_MID),
            ),
        ),
        InboundEmail(
            content_type="text/plain",
            headers={"in-reply-to": _OUTBOUND_MID},
            parts=(MimePart(content_type="text/plain"),),
        ),
    ],
)
def test_mdn_false_for_dsn_and_normal_reply(email: InboundEmail) -> None:
    # 反例：退信（只有 delivery-status 部分）与普通回复都不是 MDN。
    assert is_mdn(email) is False


def test_scan_references_in_reply_to_first_then_references_deduped() -> None:
    # In-Reply-To 指向直接父级、排在前；References 是线索链、排其后并去重
    # 保序——命中多个已知出站时以靠前者为父行。期望列表逐字符手写。
    headers = {
        "in-reply-to": "<b@example.com>",
        "references": "<a@example.com> <b@example.com> <c@example.com>",
    }
    assert scan_references(headers) == ("<b@example.com>", "<a@example.com>", "<c@example.com>")


def test_scan_references_only_references_header() -> None:
    # 只带 References（无 In-Reply-To）的回复同样可解析。
    assert scan_references({"references": "<a@example.com>"}) == ("<a@example.com>",)


def test_scan_references_missing_headers_is_no_reference() -> None:
    # design.md §3.4：no_reference = 没有 In-Reply-To 也没有 References。
    assert scan_references({}) == "no_reference"


@pytest.mark.parametrize(
    ("header_name", "value"),
    [("in-reply-to", "答复：那封邮件"), ("references", "")],
)
def test_scan_references_unparsable_is_malformed(header_name: str, value: str) -> None:
    # design.md §3.4：malformed_reference = 引用头存在但畸形到无法解析——
    # 解析不出任何 msg-id（含空值头）即属此类，而不是当作「没有引用」。
    assert scan_references({header_name: value}) == "malformed_reference"


def test_match_reference_hit_returns_known_outbound_id() -> None:
    # 引用里含本服务的出站 Message-ID → 命中并原样返回（流层再换行成 parent_id）。
    assert (
        match_reference(("<stranger@example.net>", _OUTBOUND_MID), {_OUTBOUND_MID}) == _OUTBOUND_MID
    )


def test_match_reference_miss_is_no_match() -> None:
    # design.md §3.4：no_match = 引用存在但匹配不到本服务的出站邮件。
    assert match_reference(("<stranger@example.net>",), {_OUTBOUND_MID}) == "no_match"


def test_discard_reasons_taken_from_constants_vocabulary() -> None:
    # 本模块产出的丢弃标识符（含 is_mdn / parse_dsn 语义对应的 mdn 与
    # dsn_without_id）必须都在 constants.DISCARD_REASONS 词表内——不自造。
    produced_by_core = {
        "no_reference",
        "malformed_reference",
        "no_match",
        "mdn",
        "dsn_without_id",
    }
    assert produced_by_core <= set(constants.DISCARD_REASONS)
