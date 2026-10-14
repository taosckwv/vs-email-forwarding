#!/usr/bin/env python3
"""查询邮件回复用例：keyset 游标取一页回复，并给出下一页游标。

设计真源：``design.md`` §4.1（回复查询用 keyset 游标、页大小取配置、``found`` 语义）、§2.5
（排序键 ``received_at ASC, id ASC`` 与「回复排序取服务自己的入库时刻」的口径）、§8（会话边界
与模型转换）；对外字段的唯一真源是 ``docs/api-contract.yaml``（``GET /v1/messages/{id}/replies``
的 ``RepliesPageResponse`` / ``Reply`` 与 ``after`` 的格式）。对应 ``spec.md`` 的 ``FR-10`` 与
``AC-15`` / ``AC-16`` / ``AC-31`` / ``AC-35`` / ``AC-41``；HTTP 侧完整验证按 ``plan.md``
§7.3 / §7.4 落在阶段 5 的 ``tests/integration/test_api_contract.py``（T-38）——用例层不带单测。

三个口径：**keyset 游标**（不用 ``limit`` / ``offset``：新回复到达会让偏移窗口整体漂移，
``design.md`` §4.1）→ **页大小取自配置** → **``found`` 与状态查询同一判据**：

* ``after`` 是上一页最后一行的 ``(received_at, id)``，缺省（``None``）从第一页开始；谓词是
  行值比较 ``(received_at, id) > (after)``，正好落在 ``(parent_id, received_at)`` 索引上。
* ``limit`` 缺省取配置 ``REPLIES_PAGE_SIZE``，给了也按它封顶——契约 ``limit`` 参数写明
  「缺省与上限均取服务配置」，所以这是截断而不是 422（参数形状校验在 ``api/``）。
* ``found`` 取「该 ID 是不是一封已知的**出站**邮件」，与状态查询同一 ID 空间、同一读取顺序：
  直接复用 ``query_message.py`` 的 :func:`read_outbound_message`（三次读的优先级与方向判据
  只允许有一处实现，两处各写一份必然漂移）。「存在但尚无回复」是 ``found: true`` + 空集合
  （``AC-31``，正常状态）、「ID 不存在」是 ``found: false`` + 空集合（``AC-35``），两者都不靠
  HTTP 状态码区分。

会话边界：每次调用一个 ``REPEATABLE READ`` 只读会话，「存在性」与「这一页」两次读落在同一
快照内（与状态查询同一口径，``design.md`` §4.1 / §8）；转换（ORM 行 → ``Reply``）都在会话
作用域内完成，不存在「会话已关再触发懒加载」的路径。

游标形态由契约钉死：``<received_at>,<id>``，时刻是 UTC 的 RFC 3339 六位小数 + ``Z``。本层只
**生成**游标；**解析**由 ``api/`` 完成——``api/`` 不依赖 ``db/``，而这一对值的类型是
``datetime`` + UUID，属用例层的入参形状。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import cast

from vs_email_forwarding.config import get_settings
from vs_email_forwarding.constants import TIMESTAMP_FORMAT, BodyFormat
from vs_email_forwarding.db.models import Message
from vs_email_forwarding.db.repositories.messages import replies_page
from vs_email_forwarding.db.session import session_factory
from vs_email_forwarding.schemas.messages import RepliesPageResponse, Reply
from vs_email_forwarding.service.query_message import (
    REPEATABLE_READ,
    read_outbound_message,
)

__all__ = ["query_replies"]


def _received_at_of(row: Message) -> datetime:
    """回复行的 ``received_at``：排序键，入站行写入时必带（``design.md`` §2.5）。

    列本身允许 ``NULL``（表示「某事件尚未发生」），但回复行没有到达时刻就无法排序，因此
    ``NULL`` 意味着不变量被破坏——宁可直接失败，也不猜一个时刻。
    """
    if row.received_at is None:
        raise ValueError(f"回复行缺少 received_at：{row.id}")
    return row.received_at


def _to_reply(row: Message) -> Reply:
    """ORM 行 → 契约 ``Reply``（``design.md`` §8：转换在 ``service/`` 作用域内完成）。"""
    return Reply(
        id=row.id,
        subject=row.subject,
        body=row.body,
        body_format=cast(BodyFormat, row.body_format),
        received_at=_received_at_of(row),
    )


def _cursor_of(row: Message) -> str:
    """把该行拼成 ``next_cursor``：契约 ``after`` 的 ``<received_at>,<id>`` 形态。"""
    return f"{_received_at_of(row).astimezone(UTC).strftime(TIMESTAMP_FORMAT)},{row.id}"


async def query_replies(
    message_id: uuid.UUID,
    *,
    after: tuple[datetime, uuid.UUID] | None = None,
    limit: int | None = None,
) -> RepliesPageResponse:
    """取一封出站邮件的回复页（``GET /v1/messages/{id}/replies`` 的用例）。

    ``after`` 由 ``api/`` 从查询参数解析（形态见模块 docstring）；``limit`` 是调用方按契约
    ``limit`` 参数**已经解析过形状**（整数、≥ 1）的值，本层只负责按配置封顶。会话内只有读、
    没有写，因此不需要提交。
    """
    settings = get_settings()
    page_size = (
        settings.replies_page_size if limit is None else min(limit, settings.replies_page_size)
    )

    factory = session_factory(isolation_level=REPEATABLE_READ)
    async with factory() as session:
        facts = await read_outbound_message(session, message_id)
        if facts is None:
            # 「该 ID 不存在」：成功响应 + found: false + 空集合（AC-35、契约）。
            return RepliesPageResponse(id=message_id, found=False, replies=[], next_cursor=None)

        # 多取一行只为判断「还有没有下一页」：这样 next_cursor 为 null 恰好等价于「已取完」
        # （契约对 next_cursor 的说明），客户端不必再多跑一次空页请求。
        rows = await replies_page(
            session,
            message_id,
            after=(None, None) if after is None else after,
            limit=page_size + 1,
        )
        page_rows = rows[:page_size]
        return RepliesPageResponse(
            id=message_id,
            found=True,
            replies=[_to_reply(row) for row in page_rows],
            next_cursor=_cursor_of(page_rows[-1]) if len(rows) > page_size else None,
        )
