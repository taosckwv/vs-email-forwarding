#!/usr/bin/env python3
"""提交邮件用例：受理一次提交，落一行出站待审行并返回初始状态。

设计真源：``design.md`` §3.1（提交与待审行的产生）、§8（会话边界与模型转换）；对外字段的
唯一真源是 ``docs/api-contract.yaml``（``POST /v1/messages`` 的请求 / 响应）。对应
``spec.md`` 的 ``FR-1`` / ``AC-1``，其 HTTP 侧完整验证按 ``plan.md`` §7.1 / §7.3 落在阶段 5
的 ``tests/integration/test_api_contract.py``（T-38）——用例层不带单测。

本用例只做三件事，顺序即 ``design.md`` §3.1：

1. **生成 ``Message-ID``**：``id`` 由本层新生成（UUIDv4，对外 ID 与库中主键同值）；
   ``Message-ID`` 由 ``core/message_id.py`` 的纯逻辑按「该 ``id`` + ``MAIL_FROM_ADDRESS`` 的域」
   拼出，提交时刻就定下来，之后跨表**原样搬运**——它是入站回复关联的唯一依据，掺入时间戳或
   随机数都会让关联失效。
2. **写一行出站待审行**：经 ``db/repositories/pending.py`` 的 ``insert_outbound_pending`` 写入
   （``direction = outbound``、``attempt_count = 0``、``next_attempt_at`` 建行即到点）。本层
   **不写 SQL**、不碰 ORM（``AGENTS.md`` §7、``docs/architecture.md`` §2）。
3. **返回 ``{id, status}``**：``status`` 恒为 ``submitted``——行在 ``pending_messages`` 且
   ``attempt_count = 0``（``design.md`` §2.1 落点表；契约 ``SubmitMessageResponse.status``
   写死「提交成功时恒为 ``submitted``」）。

会话边界（``design.md`` §8、``docs/architecture.md`` §5）：同步请求路径**每次调用**由
``db/session.py`` 的会话工厂开一个短会话，提交后随 ``async with`` 退出即关；事务边界在本层，
仓储只接收会话。会话工厂与配置都在本层内部取，**不经 ``api/`` 传入**：``api/`` 不得依赖
``db/``，其函数签名里也不得出现会话、ORM 或 SQLAlchemy 类型（``design.md`` §8、``plan.md``
§2.6），因此路由层递不进会话工厂；显式注入会话工厂的是 ``flows/``——它们的调用方是进程装配，
而同步路径的调用方是 HTTP 层。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Final

from vs_email_forwarding.config import get_settings
from vs_email_forwarding.constants import MESSAGE_STATE_SUBMITTED, MessageState
from vs_email_forwarding.core.message_id import build_message_id
from vs_email_forwarding.db.repositories.pending import insert_outbound_pending
from vs_email_forwarding.db.session import session_factory
from vs_email_forwarding.schemas.messages import SubmitMessageRequest, SubmitMessageResponse

__all__ = ["submit_message"]

#: 提交成功后的初始态。契约写死「提交成功时恒为 ``submitted``」（``docs/api-contract.yaml``
#: 的 ``SubmitMessageResponse.status``）；设计侧依据是 ``design.md`` §2.1 落点表首行——行刚落进
#: ``pending_messages``、``attempt_count = 0`` 即 ``submitted``。取值从 ``constants`` 取具名
#: 常量，不在本模块重复字面量。
_SUBMITTED: Final[MessageState] = MESSAGE_STATE_SUBMITTED


async def submit_message(
    request: SubmitMessageRequest,
    *,
    now: datetime | None = None,
) -> SubmitMessageResponse:
    """受理一次提交：生成 ``Message-ID`` → 写一行出站待审行 → 返回 ``{id, status}``。

    ``request`` 是**已经过校验**的请求体：必填、地址格式、取值枚举在 ``schemas/`` 边界定死，
    长度上限由 ``api/`` 按配置校验（``design.md`` §2.6 / §4.1）。本层不重复校验，也不做业务
    判定——审查与否属流①（``design.md`` §3.2）。

    ``now`` 供测试注入（注入时钟、不靠真实时间推进：``docs/testing.md`` §3.6）；缺省取一次带时区
    的当前 UTC 时间（``AGENTS.md`` §7）。这一次取时同时写进待审行的 ``created_at`` 与
    ``next_attempt_at``：前者是 ``NFR-8`` 30 天保留期的起算点，必须等于提交时刻（``design.md``
    §2.5）；后者建行即到点，流① 当轮即可认领（§2.4）。

    :class:`ValueError` 不在这里接住：``build_message_id`` 只在 ``MAIL_FROM_ADDRESS`` 解析不出
    邮件域时抛（``core/message_id.py``），而启动校验（``design.md`` §4.4 判据 2）已经拦住了这种
    配置。走到这里说明不变量被破坏——宁可直接失败，也不落一行没有 ``Message-ID`` 的待审行。
    """
    settings = get_settings()
    pending_id = uuid.uuid4()
    submitted_at = now if now is not None else datetime.now(UTC)
    message_id = build_message_id(pending_id, settings.mail_from_address)

    factory = session_factory()
    async with factory() as session:
        await insert_outbound_pending(
            session,
            pending_id=pending_id,
            message_id=message_id,
            recipient=request.recipient,
            subject=request.subject,
            body=request.body,
            body_format=request.body_format,
            now=submitted_at,
        )
        await session.commit()

    return SubmitMessageResponse(id=pending_id, status=_SUBMITTED)
