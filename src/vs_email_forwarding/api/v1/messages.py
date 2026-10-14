#!/usr/bin/env python3
"""消息端点：提交一封邮件与两个查询端点（`FR-1` / `FR-10`）。

对外字段、必填、取值枚举与取值约束的**唯一真源**是 `docs/api-contract.yaml`；本模块只做
`docs/code-style.md` §4 的三件事：**请求校验 → 调用 `service/` 用例 → 组装响应**。字段定义
不在本模块重复，业务编排与事务边界都在 `service/`（`docs/architecture.md` §2：`api/` 只认
`schemas/` 与 `service/`，不碰仓储、不直连 SMTP / IMAP，签名里不出现会话、ORM 或
SQLAlchemy 类型——`design.md` §8）。

两个校验落点与各自的判据（`design.md` §4.1 的 422 清单）：

* **`schemas/` 边界**（`SubmitMessageRequest`）：必填、非空、单收件人地址格式（禁空白，
  因此 `recipient` 的 CR / LF 也在这里被拦）、`body_format` 取值枚举、未知字段忽略
  （`extra="ignore"`，契约 `additionalProperties: true`）。
* **本模块**：`subject` 的 CR / LF（头注入）、`subject` 与 `body` 的长度上限、路径参数
  `{id}` 的 UUID 形状、`limit` 的 `minimum: 1`、`after` 的游标形态。长度上限为什么不在
  `schemas/` 里写死：那一层不依赖 `config.py`，抄成字面量会与配置分叉成一严一松两道闸门
  （`schemas/messages.py` 的模块 docstring、`AC-33`）。

所有校验失败一律 422 且**不落库**：校验全部发生在调用 `service/` 之前，被拒的请求根本走不到
写库那一步（`AC-2` / `AC-3` / `AC-33`）。HTTP 状态码只表达请求本身是否成功，不承载业务语义
（`design.md` §4.1）：查询端点的「该 ID 不存在」是 200 + `found: false`，由用例层返回。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Final

from fastapi import APIRouter, HTTPException, Path, Query, status

from vs_email_forwarding.config import get_settings
from vs_email_forwarding.constants import TIMESTAMP_FORMAT
from vs_email_forwarding.schemas.messages import (
    MessageStatusResponse,
    RepliesPageResponse,
    SubmitMessageRequest,
    SubmitMessageResponse,
)
from vs_email_forwarding.service.query_message import query_message as query_message_use_case
from vs_email_forwarding.service.query_replies import query_replies as query_replies_use_case
from vs_email_forwarding.service.submit_message import submit_message as submit_message_use_case

__all__ = ["CURSOR_PATTERN", "get_message", "list_replies", "router", "submit_message"]

router = APIRouter(tags=["messages"])

#: 请求本身不合法（422）。契约里三个端点的错误响应都是它，且契约**不固定其响应体形状**
#: （`docs/api-contract.yaml` 的 `ValidationFailed`：由实现框架生成、以实际实现为准）。
#: 取 starlette 的常量而不是字面量 `422`；用新名字是因为旧名
#: `HTTP_422_UNPROCESSABLE_ENTITY` 在当前版本已标记弃用。
_VALIDATION_FAILED: Final = status.HTTP_422_UNPROCESSABLE_CONTENT

#: `after` 的形态，逐字来自契约 `/v1/messages/{id}/replies` 里 `after` 参数的 `pattern`：
#: `<received_at>,<id>`，时刻是 UTC 的 RFC 3339 六位小数 + `Z`。形状非法即由查询参数的
#: pattern 校验按 422 拒绝（`design.md` §4.1：请求本身不合法）。
CURSOR_PATTERN: Final = (
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z,"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _parse_cursor(value: str) -> tuple[datetime, uuid.UUID]:
    """把契约游标 `<received_at>,<id>` 解析成 `service/` 用的入参形状 `(datetime, UUID)`。

    这里不再重复一次形状校验：形状已由 `after` 查询参数的 pattern 与 422 定住（见
    `CURSOR_PATTERN`）。时刻按契约就是 UTC，因此解析后直接补 `UTC` 时区（`AGENTS.md` §7
    要求时间一律带时区）。
    """
    received_at_text, _, message_id_text = value.partition(",")
    received_at = datetime.strptime(received_at_text, TIMESTAMP_FORMAT).replace(tzinfo=UTC)
    return received_at, uuid.UUID(message_id_text)


def _validate_content(request: SubmitMessageRequest) -> None:
    """主题与正文的校验：头注入（CR / LF）与长度上限；不通过即抛 422。

    上限取自配置项 `MAX_SUBJECT_CHARS`（单位字符）与 `MAX_BODY_BYTES`（单位**字节**，
    `design.md` §2.6）。错误信息只出现**配置项名**，不回显主题或正文（`AGENTS.md` §8 第 4 条
    的取向，口径同 `design.md` §4.4）。
    """
    settings = get_settings()
    if "\r" in request.subject or "\n" in request.subject:
        raise HTTPException(
            status_code=_VALIDATION_FAILED,
            detail="subject 不得含 CR / LF（头注入）",
        )
    if len(request.subject) > settings.max_subject_chars:
        raise HTTPException(
            status_code=_VALIDATION_FAILED,
            detail="subject 超过配置项 MAX_SUBJECT_CHARS 的长度上限",
        )
    if len(request.body.encode("utf-8")) > settings.max_body_bytes:
        raise HTTPException(
            status_code=_VALIDATION_FAILED,
            detail="body 超过配置项 MAX_BODY_BYTES 的长度上限",
        )


@router.post("/v1/messages", response_model=SubmitMessageResponse, summary="提交一封邮件")
async def submit_message(request: SubmitMessageRequest) -> SubmitMessageResponse:
    """受理一封邮件并立即返回 `{id, status}`（`FR-1` / `AC-1` 的入口）。

    校验通过后写一行待审行即返回，投递由后台流异步完成——本端点不等待投递结果。发件人
    `From` 与回复地址 `Reply-To` 一律取自服务配置，请求体里的同名未知字段被忽略（`AC-4`
    的 `From` 固定由 `providers/smtp/` 兑现，`design.md` §4.3）。
    """
    _validate_content(request)
    return await submit_message_use_case(request)


@router.get(
    "/v1/messages/{id}",
    response_model=MessageStatusResponse,
    summary="查询一封邮件的当前状态",
)
async def get_message(message_id: Annotated[uuid.UUID, Path(alias="id")]) -> MessageStatusResponse:
    """查询一封**出站**邮件的当前状态（`FR-10`）。

    路径参数不是一个合法 UUID 的请求到不了这里：框架按 422 拒绝（`design.md` §4.1）。
    「该 ID 不存在」不是错误——用例返回 200 + `found: false`（`AC-35`）。
    """
    return await query_message_use_case(message_id)


@router.get(
    "/v1/messages/{id}/replies",
    response_model=RepliesPageResponse,
    summary="查询一封邮件的全部回复",
)
async def list_replies(
    message_id: Annotated[uuid.UUID, Path(alias="id")],
    limit: Annotated[
        int | None,
        Query(ge=1, description="本页最多返回多少条；缺省与上限均取配置 REPLIES_PAGE_SIZE。"),
    ] = None,
    after: Annotated[
        str | None,
        Query(pattern=CURSOR_PATTERN, description="上一页响应里的 next_cursor 原样传入。"),
    ] = None,
) -> RepliesPageResponse:
    """取一封出站邮件的回复页（`FR-10` / `AC-15`）。

    `limit` 在这里只校验契约的 `minimum: 1`（`limit <= 0` 即 422）；**按配置封顶由用例层
    完成**——契约写明「缺省与上限均取服务配置 `REPLIES_PAGE_SIZE`」，因此超过上限是截断，
    不是 422（`design.md` §4.1）。`after` 对客户端是不透明游标，解析落点在本层：用例层的入参
    形状是 `(received_at, id)`，而 `api/` 不依赖 `db/`（`query_replies.py` 的模块 docstring）。
    """
    cursor = _parse_cursor(after) if after is not None else None
    return await query_replies_use_case(message_id, after=cursor, limit=limit)
