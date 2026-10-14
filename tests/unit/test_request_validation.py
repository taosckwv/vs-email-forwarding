#!/usr/bin/env python3
"""三个消息端点的请求校验单元测试（`T-30`）。

判据与期望值全部来自 `docs/api-contract.yaml`（必填字段清单、单收件人地址 `pattern`、
`body_format` 的 `enum`、`limit` 的 `minimum: 1`、`after` 的 `pattern`）与 `design.md`
§4.1 的 422 清单；长度上限的期望值来自**配置项** `MAX_SUBJECT_CHARS` / `MAX_BODY_BYTES`
（`AC-33`：上限随配置变），不来自被测实现。占位地址一律 `example.com`（`AGENTS.md` §8
第 4 条）。

`api/` 的两条边界在这里各用一种替身隔开（`docs/testing.md` §3.5 允许的落点）：

* **数据库**——`service/` 用例就是读写数据库的那一步，而本目录不得触碰数据库。因此三个
  用例名在本模块里被替换成进程内的假用例，并记录它们收到的请求：这就是「422 的请求根本
  走不到写库那一步」的可观察判据（`AC-2` / `AC-3` / `AC-33` 的「且库中不新增记录」）。
* **配置**——`small_limits` 夹具用小上限的 `Settings` 替换 `get_settings`，用来在长度上限的
  **边界两侧**各钉一条断言（上限值由配置给，不由代码常量给）。

HTTP 侧走进程内 `fastapi.testclient.TestClient`（`tests/unit/README.md` 允许），不启动
uvicorn、不连 PostgreSQL。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from vs_email_forwarding.api.v1 import messages
from vs_email_forwarding.config import Settings
from vs_email_forwarding.main import create_app
from vs_email_forwarding.schemas.messages import (
    RepliesPageResponse,
    SubmitMessageRequest,
    SubmitMessageResponse,
)

#: 假用例返回的 ID 与状态：提交成功时状态恒为 `submitted`（契约 `SubmitMessageResponse.status`）。
_FIXED_ID = uuid.UUID("00000000-0000-4000-8000-000000000000")

#: 一份形状合法的提交请求体：主题 2 字符、正文 6 字节，都在下面夹具的小上限（5 / 8）之内。
_VALID_SUBMIT: dict[str, object] = {
    "recipient": "colleague@example.com",
    "subject": "进展",
    "body": "正文",
}

#: 一份形状合法的 `after` 游标：`<received_at>,<id>`，时刻是 UTC 的 RFC 3339 六位小数 + `Z`
#: （契约 `/v1/messages/{id}/replies` 的 `after.pattern`）。
_CURSOR = "2030-01-02T03:04:05.000000Z,00000000-0000-4000-8000-000000000000"


def _submit_payload(**overrides: object) -> dict[str, object]:
    """一份合法的提交请求体，按用例覆盖其中一两项。"""
    return {**_VALID_SUBMIT, **overrides}


@pytest.fixture()
def small_limits(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """把长度上限换成一对小值（主题 5 字符 / 正文 8 字节），供边界两侧的用例使用。"""
    settings = Settings(max_subject_chars=5, max_body_bytes=8)
    monkeypatch.setattr(messages, "get_settings", lambda: settings)
    return settings


@pytest.fixture()
def submit_calls(monkeypatch: pytest.MonkeyPatch) -> list[SubmitMessageRequest]:
    """用假用例替掉提交用例（单测不碰数据库），并记录它收到的请求体。"""
    calls: list[SubmitMessageRequest] = []

    async def fake_submit_message(request: SubmitMessageRequest) -> SubmitMessageResponse:
        calls.append(request)
        return SubmitMessageResponse(id=_FIXED_ID, status="submitted")

    monkeypatch.setattr(messages, "submit_message_use_case", fake_submit_message)
    return calls


@pytest.fixture()
def replies_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """用假用例替掉回复查询用例（单测不碰数据库），并记录它收到的入参。"""
    calls: list[dict[str, object]] = []

    async def fake_query_replies(
        message_id: uuid.UUID,
        *,
        after: tuple[datetime, uuid.UUID] | None = None,
        limit: int | None = None,
    ) -> RepliesPageResponse:
        calls.append({"message_id": message_id, "after": after, "limit": limit})
        return RepliesPageResponse(id=message_id, found=False, replies=[], next_cursor=None)

    monkeypatch.setattr(messages, "query_replies_use_case", fake_query_replies)
    return calls


# --- 路由注册（契约 paths 里的三个 messages 端点） --------------------------------


def test_three_message_routes_registered() -> None:
    """三端点按契约的路径与方法注册，鉴权占位之外的查询 / 提交入口由此成立。"""
    paths = create_app().openapi()["paths"]

    assert "post" in paths["/v1/messages"]
    assert "get" in paths["/v1/messages/{id}"]
    assert "get" in paths["/v1/messages/{id}/replies"]


# --- 必填与单收件人（契约 required / recipient.pattern；AC-2 / AC-3） --------------


def test_submit_missing_recipient_422(client: TestClient) -> None:
    """缺收件人 → 422，且响应体里出现收件人字段名（`AC-2`）。"""
    payload = _submit_payload()
    del payload["recipient"]

    response = client.post("/v1/messages", json=payload)

    assert response.status_code == 422
    assert "recipient" in response.text


def test_submit_two_recipients_as_array_422(client: TestClient) -> None:
    """收件人给成数组（两个地址）→ 422（`AC-3`）。"""
    response = client.post(
        "/v1/messages",
        json=_submit_payload(recipient=["a@example.com", "b@example.com"]),
    )

    assert response.status_code == 422


def test_submit_two_recipients_comma_separated_422(client: TestClient) -> None:
    """收件人给成逗号分隔的多地址 → 422（`AC-3`）。"""
    response = client.post(
        "/v1/messages",
        json=_submit_payload(recipient="a@example.com,b@example.com"),
    )

    assert response.status_code == 422


def test_submit_recipient_without_at_422(client: TestClient) -> None:
    """地址非法（无 `@`）→ 422（`design.md` §4.1 的 422 清单）。"""
    response = client.post("/v1/messages", json=_submit_payload(recipient="example.com"))

    assert response.status_code == 422


def test_submit_recipient_with_whitespace_422(client: TestClient) -> None:
    """地址非法（含空白）→ 422（`design.md` §4.1）。"""
    response = client.post("/v1/messages", json=_submit_payload(recipient="col league@example.com"))

    assert response.status_code == 422


def test_submit_recipient_with_crlf_422(client: TestClient) -> None:
    """收件人含 CR / LF（头注入）→ 422（`design.md` §4.1、`docs/security.md`）。"""
    response = client.post(
        "/v1/messages",
        json=_submit_payload(recipient="colleague@example.com\r\nBcc: attacker@example.com"),
    )

    assert response.status_code == 422


def test_submit_subject_with_crlf_422(client: TestClient) -> None:
    """主题含 CR / LF（头注入）→ 422（`design.md` §4.1）。"""
    response = client.post(
        "/v1/messages",
        json=_submit_payload(subject="进展\r\nBcc: attacker@example.com"),
    )

    assert response.status_code == 422


def test_submit_subject_with_carriage_return_only_422(
    client: TestClient, submit_calls: list[SubmitMessageRequest]
) -> None:
    """主题**仅含 CR**（无 LF）→ 422，且不落库。

    与上面那条两字符同时出现的用例配对：`design.md` §4.1 的「`recipient` 或 `subject` 含
    CR/LF」按**字符各自独立**成立，`docs/security.md` 的头注入条目说的是「头 / 地址字段未做
    校验即拼入 MIME 或 SMTP 命令」（CR 单独出现就足以截断头）。只测两字符同时出现，判据被写成
    「必须同时出现才拒」（`or` → `and`）也照样全绿——真漏测，这两条边界用例专治它。
    """
    response = client.post(
        "/v1/messages",
        json=_submit_payload(subject="进展\rBcc: attacker@example.com"),
    )

    assert response.status_code == 422
    assert submit_calls == []


def test_submit_subject_with_line_feed_only_422(
    client: TestClient, submit_calls: list[SubmitMessageRequest]
) -> None:
    """主题**仅含 LF**（无 CR）→ 422，且不落库（同上，LF 单独出现即头注入）。"""
    response = client.post(
        "/v1/messages",
        json=_submit_payload(subject="进展\nBcc: attacker@example.com"),
    )

    assert response.status_code == 422
    assert submit_calls == []


def test_submit_body_format_outside_contract_enum_422(client: TestClient) -> None:
    """`body_format` 不在契约枚举 `[text, html]` 内 → 422（`design.md` §4.1）。"""
    response = client.post("/v1/messages", json=_submit_payload(body_format="markdown"))

    assert response.status_code == 422


# --- 长度上限（配置项 MAX_SUBJECT_CHARS / MAX_BODY_BYTES；AC-33） ------------------


def test_submit_subject_over_config_limit_422(client: TestClient, small_limits: Settings) -> None:
    """主题字符数超过配置上限（6 > 5）→ 422，错误信息只出现配置项名、不回显主题。"""
    response = client.post("/v1/messages", json=_submit_payload(subject="主题主题主题"))

    assert response.status_code == 422
    assert "MAX_SUBJECT_CHARS" in response.text


def test_submit_subject_at_config_limit_accepted(
    client: TestClient, small_limits: Settings, submit_calls: list[SubmitMessageRequest]
) -> None:
    """主题字符数恰好等于配置上限（5 == 5）→ 放行（边界两侧都要测）。"""
    response = client.post("/v1/messages", json=_submit_payload(subject="主题主题主"))

    assert response.status_code == 200


def test_submit_body_over_config_limit_counts_bytes_not_chars(
    client: TestClient, small_limits: Settings
) -> None:
    """正文上限单位是**字节**：3 个汉字只有 3 字符、却有 9 字节 > 8 → 422。"""
    response = client.post("/v1/messages", json=_submit_payload(body="正正正"))

    assert response.status_code == 422
    assert "MAX_BODY_BYTES" in response.text


def test_submit_body_at_config_limit_accepted(
    client: TestClient, small_limits: Settings, submit_calls: list[SubmitMessageRequest]
) -> None:
    """正文恰好 8 字节（8 == 8）→ 放行（边界两侧都要测）。"""
    response = client.post("/v1/messages", json=_submit_payload(body="abcdefgh"))

    assert response.status_code == 200


# --- 校验失败不落库：请求根本到不了 service/ 用例（AC-2 / AC-3 / AC-33） ----------


def test_submit_rejected_request_never_reaches_service(
    client: TestClient, submit_calls: list[SubmitMessageRequest]
) -> None:
    """多收件人的请求 → 422，且没有走到写库那一步（用例未被调用）。"""
    response = client.post(
        "/v1/messages",
        json=_submit_payload(recipient="a@example.com,b@example.com"),
    )

    assert response.status_code == 422
    assert submit_calls == []


def test_submit_valid_request_is_handed_to_use_case(
    client: TestClient, small_limits: Settings, submit_calls: list[SubmitMessageRequest]
) -> None:
    """合法请求（含未知的 `from` 字段）→ 200：未知字段被忽略，请求原样交给用例。"""
    response = client.post(
        "/v1/messages",
        json={
            **_submit_payload(subject="主题主题主", body="abcdefgh"),
            "from": "attacker@example.com",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"id": str(_FIXED_ID), "status": "submitted"}
    assert len(submit_calls) == 1
    sent = submit_calls[0]
    assert sent.recipient == "colleague@example.com"
    assert sent.subject == "主题主题主"
    assert sent.body == "abcdefgh"
    assert sent.body_format == "text"


# --- 查询端点：路径参数、limit 最小值与游标形态（design.md §4.1、契约参数定义） ---


def test_get_message_invalid_uuid_422(client: TestClient) -> None:
    """路径参数 `{id}` 不是合法 UUID → 422（`design.md` §4.1）。"""
    response = client.get("/v1/messages/not-a-uuid")

    assert response.status_code == 422


def test_replies_limit_below_contract_minimum_422(
    client: TestClient, replies_calls: list[dict[str, object]]
) -> None:
    """`limit=0` 低于契约 `minimum: 1` → 422（不靠用例层的 `min()` 截断兜底）。"""
    response = client.get(f"/v1/messages/{_FIXED_ID}/replies", params={"limit": 0})

    assert response.status_code == 422
    assert replies_calls == []


def test_replies_limit_at_contract_minimum_accepted(
    client: TestClient, replies_calls: list[dict[str, object]]
) -> None:
    """`limit=1` 恰好等于契约最小值 → 放行（边界两侧都要测）。"""
    response = client.get(f"/v1/messages/{_FIXED_ID}/replies", params={"limit": 1})

    assert response.status_code == 200


def test_replies_after_malformed_cursor_422(
    client: TestClient, replies_calls: list[dict[str, object]]
) -> None:
    """`after` 不符合契约 `pattern` → 422（请求本身不合法）。"""
    response = client.get(f"/v1/messages/{_FIXED_ID}/replies", params={"after": "2030-01-02,abc"})

    assert response.status_code == 422
    assert replies_calls == []


def test_replies_parses_cursor_and_forwards_limit(
    client: TestClient, replies_calls: list[dict[str, object]]
) -> None:
    """`after` 由 api 层解析成用例层的 `(received_at, id)`，`limit` 原样交给用例。"""
    response = client.get(
        f"/v1/messages/{_FIXED_ID}/replies",
        params={"limit": 3, "after": _CURSOR},
    )

    assert response.status_code == 200
    assert len(replies_calls) == 1
    assert replies_calls[0]["message_id"] == _FIXED_ID
    assert replies_calls[0]["limit"] == 3
    assert replies_calls[0]["after"] == (
        datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC),
        _FIXED_ID,
    )
