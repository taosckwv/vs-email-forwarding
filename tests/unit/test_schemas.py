#!/usr/bin/env python3
"""``schemas/messages.py`` 的模型形状与时间序列化单测（`FR-11` / `AC-17` / `NFR-7`）。

覆盖：三个响应模型与回复元素的**字段集合与必填项**、提交请求的取值约束
（单收件人、非空、未知字段忽略）、六态取值来自 ``constants``、以及契约
``Timestamp`` 的序列化形态（UTC、六位小数、``Z`` 结尾）。

期望值来源（`docs/testing.md` R2，全部为规格推导的字面量，**不由被测代码生成**）：

* 字段集合与必填：``docs/api-contract.yaml`` 的 ``components.schemas``——
  ``SubmitMessageRequest``（required ``recipient`` / ``subject`` / ``body``，
  可选项只有 ``body_format``）、``SubmitMessageResponse``（``id`` / ``status``）、
  ``MessageStatusResponse``（六字段全必填，``status`` 与三个时间字段可空）、
  ``RepliesPageResponse``（``id`` / ``found`` / ``replies`` / ``next_cursor``）、
  ``Reply``（五个字段全必填）；
* 取值约束：同一份契约的 ``pattern`` / ``enum`` / ``maxLength`` 描述与
  ``design.md`` §4.1 的 422 清单（单收件人、地址非法、未知字段忽略）；
* 六态取值：`design.md` §2.3 的状态机与契约 ``MessageState``；运行期取值的
  唯一出处是 ``vs_email_forwarding.constants``（本文件只从那里取全集）；
* 时间格式：`design.md` §4.1 / §2.5 与契约 ``Timestamp``——UTC 的 RFC 3339、
  固定六位小数、``Z`` 结尾。

**长度上限不在本文件的期望值里**：``MAX_SUBJECT_CHARS`` / ``MAX_BODY_BYTES`` 是
配置项、由 API 层校验（``design.md`` §2.6，落点 `AC-33`），模型层不得依赖
``config.py``（``docs/architecture.md`` §2），因此这里不断言上限。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from vs_email_forwarding import constants
from vs_email_forwarding.schemas.messages import (
    MessageStatusResponse,
    RepliesPageResponse,
    Reply,
    SubmitMessageRequest,
    SubmitMessageResponse,
)

#: 契约里的固定占位 ID（``MessageId`` = string/format uuid），与库中主键同类型。
_MESSAGE_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")

#: 一个合法的单收件人地址；地址一律用占位域（`docs/testing.md` §4.7）。
_ADDRESS = "first.last+tag@example.com"


def _status_response(**overrides: object) -> MessageStatusResponse:
    """按契约的必填六字段构造状态响应；用例只覆写自己要判定的那一个。"""
    payload: dict[str, object] = {
        "id": _MESSAGE_ID,
        "found": True,
        "status": "submitted",
        "created_at": datetime(2030, 1, 1, tzinfo=UTC),
        "sent_at": None,
        "reviewed_at": None,
    }
    payload.update(overrides)
    return MessageStatusResponse.model_validate(payload)


# ---------------------------------------------------------------------------
# 字段集合与必填项（契约 components.schemas 逐字段）
# ---------------------------------------------------------------------------


def test_submit_request_field_set_matches_contract() -> None:
    # 契约 SubmitMessageRequest：required [recipient, subject, body]，
    # 唯一可选项 body_format（默认 text）。
    assert set(SubmitMessageRequest.model_fields) == {
        "recipient",
        "subject",
        "body",
        "body_format",
    }
    required = {
        name for name, field in SubmitMessageRequest.model_fields.items() if field.is_required()
    }
    assert required == {"recipient", "subject", "body"}


def test_submit_response_field_set_matches_contract() -> None:
    # 契约 SubmitMessageResponse：required [id, status]，无其它字段。
    assert set(SubmitMessageResponse.model_fields) == {"id", "status"}
    assert all(field.is_required() for field in SubmitMessageResponse.model_fields.values())


def test_status_response_field_set_matches_contract() -> None:
    # 契约 MessageStatusResponse：required [id, found, status, created_at, sent_at, reviewed_at]——
    # 六个字段全部必填，其中 status 与三个时间字段可为 null（"不存在" 与 "尚未发生" 都用 null 表达）。
    assert set(MessageStatusResponse.model_fields) == {
        "id",
        "found",
        "status",
        "created_at",
        "sent_at",
        "reviewed_at",
    }
    assert all(field.is_required() for field in MessageStatusResponse.model_fields.values())


def test_replies_page_field_set_matches_contract() -> None:
    # 契约 RepliesPageResponse：required [id, found, replies, next_cursor]。
    assert set(RepliesPageResponse.model_fields) == {"id", "found", "replies", "next_cursor"}
    assert all(field.is_required() for field in RepliesPageResponse.model_fields.values())


def test_reply_field_set_matches_contract() -> None:
    # 契约 Reply：required [id, subject, body, body_format, received_at]，无别的字段。
    assert set(Reply.model_fields) == {"id", "subject", "body", "body_format", "received_at"}
    assert all(field.is_required() for field in Reply.model_fields.values())


def test_id_fields_are_uuid() -> None:
    # 契约 MessageId：string + format uuid，即库中主键（design.md §2.1）——模型与库同一类型，
    # 非法 UUID 在模型边界即被拒。
    assert SubmitMessageResponse(id=_MESSAGE_ID, status="submitted").id == _MESSAGE_ID
    with pytest.raises(ValidationError):
        SubmitMessageResponse(id="not-a-uuid", status="submitted")


# ---------------------------------------------------------------------------
# 提交请求的取值约束
# ---------------------------------------------------------------------------


def test_recipient_accepts_single_valid_address() -> None:
    # 边界另一侧：合法地址（含 . 与 +）必须被接受，避免把地址格式卡得过严。
    request = SubmitMessageRequest(recipient=_ADDRESS, subject="主题", body="正文")
    assert request.recipient == _ADDRESS


def test_recipient_rejects_comma_separated_addresses() -> None:
    # 契约 recipient 的 pattern 不含逗号，design.md §4.1：逗号分隔的多地址一律 422。
    with pytest.raises(ValidationError):
        SubmitMessageRequest(
            recipient="first@example.com,second@example.com", subject="主题", body="正文"
        )


def test_recipient_rejects_comma_inside_a_single_at_address() -> None:
    # 契约 pattern 的本地部分与域都排除逗号（`[^@\s,]+`）。`first,second@example.com`
    # 只有一个 @：不排除逗号时它会被当成合法单地址收下，正是「逗号分隔多地址」的漏网形态。
    with pytest.raises(ValidationError):
        SubmitMessageRequest(recipient="first,second@example.com", subject="主题", body="正文")


def test_recipient_rejects_list_form() -> None:
    # AC-3：收件人给成数组同样 422（契约 recipient 是 string，不是 array）。
    with pytest.raises(ValidationError):
        SubmitMessageRequest.model_validate(
            {
                "recipient": ["first@example.com", "second@example.com"],
                "subject": "主题",
                "body": "正文",
            }
        )


@pytest.mark.parametrize(
    "recipient",
    [
        "no-at-sign",  # 地址非法：无 @
        "first@second@example.com",  # 地址非法：两个 @
        "first@exa mple.com",  # 地址非法：含空白
        "first@example.com\r\nBcc: hidden@example.com",  # 头注入：收件人不得含 CR / LF
    ],
)
def test_recipient_rejects_malformed_address(recipient: str) -> None:
    # 契约 recipient 的 pattern `^[^@\s,]+@[^@\s,]+$`；design.md §4.1 的 422 清单。
    with pytest.raises(ValidationError):
        SubmitMessageRequest(recipient=recipient, subject="主题", body="正文")


@pytest.mark.parametrize("missing", ["recipient", "subject", "body"])
def test_request_rejects_missing_required_field(missing: str) -> None:
    # AC-2：缺少必填字段即 422（模型边界先拦下，不落库）。
    payload = {"recipient": _ADDRESS, "subject": "主题", "body": "正文"}
    payload.pop(missing)
    with pytest.raises(ValidationError):
        SubmitMessageRequest.model_validate(payload)


@pytest.mark.parametrize("empty_field", ["recipient", "subject", "body"])
def test_request_rejects_empty_string(empty_field: str) -> None:
    # 契约三个字段都是 minLength: 1——空串不是「没给」，同样必须被拒。
    payload = {"recipient": _ADDRESS, "subject": "主题", "body": "正文"}
    payload[empty_field] = ""
    with pytest.raises(ValidationError):
        SubmitMessageRequest.model_validate(payload)


def test_submit_request_ignores_unknown_fields() -> None:
    # 契约 additionalProperties: true + design.md §4.1：未知字段（含调用方试图指定的发件人）
    # 一律忽略、不报错，且不进入模型（AC-4 由「忽略」而非专门逻辑成立）。
    request = SubmitMessageRequest.model_validate(
        {
            "recipient": _ADDRESS,
            "subject": "主题",
            "body": "正文",
            "from": "attacker@example.com",
        }
    )
    assert "from" not in request.model_dump()


# ---------------------------------------------------------------------------
# 正文格式与六态取值
# ---------------------------------------------------------------------------


def test_body_format_defaults_to_text() -> None:
    # 契约 body_format 的 default: text（与 constants.DEFAULT_BODY_FORMAT 同值）。
    request = SubmitMessageRequest(recipient=_ADDRESS, subject="主题", body="正文")
    assert request.body_format == "text"


@pytest.mark.parametrize("body_format", constants.BODY_FORMATS)
def test_body_format_accepts_contract_enum(body_format: str) -> None:
    # 契约 BodyFormat.enum（= constants.BODY_FORMATS：text / html）。
    request = SubmitMessageRequest(
        recipient=_ADDRESS, subject="主题", body="正文", body_format=body_format
    )
    assert request.body_format == body_format


def test_body_format_rejects_value_outside_contract_enum() -> None:
    # 契约枚举只有 text / html；markdown 之类的取值一律 422（design.md §4.1）。
    with pytest.raises(ValidationError):
        SubmitMessageRequest(
            recipient=_ADDRESS, subject="主题", body="正文", body_format="markdown"
        )


@pytest.mark.parametrize("state", constants.MESSAGE_STATES)
def test_status_accepts_each_of_six_states(state: str) -> None:
    # 六态是库内与对外同一套取值、不存在映射层（design.md §2.3）：
    # 每个取值都必须能原样进响应模型。
    assert _status_response(status=state).status == state


@pytest.mark.parametrize("not_an_outbound_state", ["received", "queued"])
def test_status_rejects_values_outside_six_states(not_an_outbound_state: str) -> None:
    # 契约 MessageState.enum 只有六态：received 是入站行终态、不属于出站状态机
    # （design.md §2.3），queued 之类的旧取值更不在词表里。
    with pytest.raises(ValidationError):
        _status_response(status=not_an_outbound_state)


def test_submit_response_status_uses_the_same_six_state_vocabulary() -> None:
    # 契约 SubmitMessageResponse.status 引用同一个 MessageState：提交响应不得接受六态以外的取值。
    with pytest.raises(ValidationError):
        SubmitMessageResponse(id=_MESSAGE_ID, status="queued")


def test_reply_body_format_uses_the_contract_enum() -> None:
    # 契约 Reply.body_format 引用同一个 BodyFormat：回复响应同样只认 text / html。
    with pytest.raises(ValidationError):
        Reply(
            id=_MESSAGE_ID,
            subject="回复主题",
            body="回复正文",
            body_format="markdown",
            received_at=datetime(2030, 2, 2, 3, 4, 5, tzinfo=UTC),
        )


# ---------------------------------------------------------------------------
# 时间字段的序列化（契约 Timestamp）
# ---------------------------------------------------------------------------


def test_status_timestamps_serialize_as_utc_rfc3339_six_digits_z() -> None:
    # design.md §4.1：序列化时先转 UTC 再补零到六位。期望值手算：
    # UTC+8 的 08:00:00.123456 = UTC 的 00:00:00.123456；09:30 = 01:30；08:15:00.5 = 00:15:00.5。
    cst = timezone(timedelta(hours=8))
    dumped = _status_response(
        created_at=datetime(2030, 1, 1, 8, 0, 0, 123456, tzinfo=cst),
        sent_at=datetime(2030, 1, 1, 9, 30, 0, 0, tzinfo=cst),
        reviewed_at=datetime(2030, 1, 1, 8, 15, 0, 500000, tzinfo=cst),
    ).model_dump(mode="json")
    assert dumped["created_at"] == "2030-01-01T00:00:00.123456Z"
    assert dumped["sent_at"] == "2030-01-01T01:30:00.000000Z"
    assert dumped["reviewed_at"] == "2030-01-01T00:15:00.500000Z"


def test_reply_received_at_serializes_in_same_format() -> None:
    # 回复元素的时间字段用同一套口径；整微秒（无小数）也必须补零到六位。
    reply = Reply(
        id=_MESSAGE_ID,
        subject="回复主题",
        body="回复正文",
        body_format="text",
        received_at=datetime(2030, 2, 2, 3, 4, 5, 600000, tzinfo=UTC),
    )
    assert reply.model_dump(mode="json")["received_at"] == "2030-02-02T03:04:05.600000Z"


def test_naive_timestamp_is_rejected_instead_of_reinterpreted() -> None:
    # AGENTS.md §7 要求带时区的时间：裸时间会被 astimezone 当作宿主本地时区，
    # 静默序列化出错误时刻，因此必须在序列化边界直接失败。
    response = _status_response(
        created_at=datetime(2030, 1, 1, 0, 0, 0),  # 本用例就是要构造一个裸时间
    )
    with pytest.raises(ValueError, match="带时区"):
        response.model_dump(mode="json")


# ---------------------------------------------------------------------------
# 「查不到」与「已取完」的响应形状
# ---------------------------------------------------------------------------


def test_status_response_not_found_serializes_nulls() -> None:
    # 契约 MessageStatusResponse：found: false 时 status 为 null；尚未发生的时刻也是 null
    # （design.md §4.1：「不存在」不是错误，不用状态码区分）。
    dumped = _status_response(
        found=False, status=None, created_at=None, sent_at=None, reviewed_at=None
    ).model_dump(mode="json")
    assert dumped == {
        "id": str(_MESSAGE_ID),
        "found": False,
        "status": None,
        "created_at": None,
        "sent_at": None,
        "reviewed_at": None,
    }


def test_replies_page_cursor_is_string_or_null() -> None:
    # 契约 RepliesPageResponse.next_cursor: type [string, "null"]，格式 `<received_at>,<id>`；
    # next_cursor 为 null 表示已取完（replies 为空集合时同为 null）。
    cursor = "2030-02-02T03:04:05.600000Z,11111111-1111-4111-8111-111111111111"
    page = RepliesPageResponse(id=_MESSAGE_ID, found=True, replies=[], next_cursor=cursor)
    assert page.next_cursor == cursor
    assert (
        RepliesPageResponse(id=_MESSAGE_ID, found=True, replies=[], next_cursor=None).model_dump(
            mode="json"
        )["next_cursor"]
        is None
    )
