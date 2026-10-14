#!/usr/bin/env python3
"""对外 HTTP 请求 / 响应模型（字段唯一真源：``docs/api-contract.yaml``）。

本模块是**纯 Pydantic 模型**：字段、类型、必填与取值约束、时间序列化口径。
字段清单与取值约束**逐字段照抄契约**（契约文件开头的编写约束：``design.md`` /
``plan.md`` / ``schemas/`` 一律只引用它，不在别处重复字段定义），本模块不新增、
不改名、不放宽任何一条。业务规则不在这里：状态怎么判定在 ``core/states.py``，
校验与用例怎么编排在 ``service/`` 与 ``api/``。

不放 ORM 模型（在 ``vs_email_forwarding.db``）；本层的允许依赖为「无」
（``docs/architecture.md`` §2），只 import 共享叶子层 ``constants``。

## 长度上限为什么不进模型

``MAX_SUBJECT_CHARS`` / ``MAX_BODY_BYTES`` 是**配置项**（``design.md`` §2.6），
而本层不得依赖 ``config.py``（``docs/architecture.md`` §2）。把上限抄成字面量会
与配置分叉成一严一松两道闸门——``AC-33`` 明确要求「改配置后上限随之变」。因此
模型只做**与配置无关**的形状约束（必填、非空、地址格式、取值枚举），长度上限
一律由 API 层按配置校验（``design.md`` §2.6 / §4.1）。

## 时间字段的序列化口径

契约 ``Timestamp`` 固定为 UTC 的 RFC 3339、**六位小数 + ``Z``**，与库中
``timestamptz`` 一一对应，调用方不必处理本地时区或可变的小数位数
（``NFR-7``、``AC-17``）；格式串的唯一出处是 ``constants.TIMESTAMP_FORMAT``。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

from vs_email_forwarding.constants import (
    DEFAULT_BODY_FORMAT,
    TIMESTAMP_FORMAT,
    BodyFormat,
    MessageState,
)

__all__ = [
    "RECIPIENT_PATTERN",
    "MessageStatusResponse",
    "RepliesPageResponse",
    "Reply",
    "SubmitMessageRequest",
    "SubmitMessageResponse",
    "Timestamp",
]

#: 单个收件人地址：恰一个 ``@``、无空白、无逗号——数组或逗号分隔的多地址因此在模型
#: 边界即被拒（``AC-3``）；空白一并覆盖 CR / LF，堵住头注入（``design.md`` §4.1）。
#: 文本逐字来自契约 ``SubmitMessageRequest.recipient.pattern``。
RECIPIENT_PATTERN: str = r"^[^@\s,]+@[^@\s,]+$"


def _format_timestamp(value: datetime) -> str:
    """把时间序列化为契约 ``Timestamp``：先转 UTC，再补零到六位并以 ``Z`` 结尾。

    裸时间（无时区）**直接拒绝**而不是当成本地时区：``astimezone`` 会把裸时间解释为
    宿主本地时区，静默序列化出一个错误的时刻；``AGENTS.md`` §7 要求时间一律带时区，
    因此走到这里说明调用方破坏了不变量，宁可直接失败。
    """
    if value.tzinfo is None:
        raise ValueError("时间字段必须是带时区的 datetime（AGENTS.md §7）")
    return value.astimezone(UTC).strftime(TIMESTAMP_FORMAT)


#: 契约 ``Timestamp`` 的 Python 侧类型与序列化口径（形态见模块 docstring）。
Timestamp = Annotated[
    datetime,
    PlainSerializer(_format_timestamp, return_type=str, when_used="always"),
]


class SubmitMessageRequest(BaseModel):
    """``POST /v1/messages`` 的请求体。

    ``From`` / ``Reply-To`` 一律取自服务配置，因此请求体里出现的同名未知字段
    **忽略、不报错**（``extra="ignore"``，契约 ``additionalProperties: true``）：
    携带发件人字段也不会生效（``AC-4``）。长度上限见模块 docstring——由 API 层
    按配置校验，不在本模型里写死。
    """

    model_config = ConfigDict(extra="ignore")

    recipient: str = Field(
        min_length=1,
        pattern=RECIPIENT_PATTERN,
        description="单个收件人地址；数组或逗号分隔的多地址一律拒绝。",
    )
    subject: str = Field(min_length=1, description="邮件主题；长度上限由 API 层按配置校验。")
    body: str = Field(min_length=1, description="邮件正文；长度上限由 API 层按配置校验。")
    body_format: BodyFormat = DEFAULT_BODY_FORMAT


class SubmitMessageResponse(BaseModel):
    """``POST /v1/messages`` 的 200 响应体：受理成功时的 ``id`` 与初始状态。"""

    id: uuid.UUID
    status: MessageState


class MessageStatusResponse(BaseModel):
    """``GET /v1/messages/{id}`` 的 200 响应体。

    ``found`` 显式标识该 ID 是否存在——「不存在」不是错误，因此不用 HTTP 状态码
    区分（``design.md`` §4.1）；``found`` 为 ``false`` 时 ``status`` 为 ``null``，
    尚未发生的事件时刻同为 ``null``。六个字段都是必填字段（可空 ≠ 可省略）。
    """

    id: uuid.UUID
    found: bool
    status: MessageState | None
    created_at: Timestamp | None
    sent_at: Timestamp | None
    reviewed_at: Timestamp | None


class Reply(BaseModel):
    """一封入站回复（``RepliesPageResponse.replies`` 的元素）。"""

    id: uuid.UUID
    subject: str
    body: str
    body_format: BodyFormat
    received_at: Timestamp


class RepliesPageResponse(BaseModel):
    """``GET /v1/messages/{id}/replies`` 的 200 响应体（keyset 游标的一页）。

    ``next_cursor`` 为 ``null`` 表示已取完，客户端只需把它原样回传给 ``after``；
    分页与排序口径见 ``design.md`` §4.1，本模型只承载字段。
    """

    id: uuid.UUID
    found: bool
    replies: list[Reply]
    next_cursor: str | None
