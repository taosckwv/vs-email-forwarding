#!/usr/bin/env python3
"""查询邮件状态用例：单个事务快照内判定六态，并把 ORM 行转成对外响应。

设计真源：``design.md`` §4.1（判定表、``found`` 语义、「三次查询同一快照」）、§2.5（时间字段
语义）、§8（会话边界与模型转换）；对外字段的唯一真源是 ``docs/api-contract.yaml``
（``GET /v1/messages/{id}`` 的响应与 ``MessageId``）。对应 ``spec.md`` 的 ``FR-10`` 与
``AC-35``（「不存在」与「存在但还没到某一步」都成功、靠 ``found`` 区分）；HTTP 侧完整验证按
``plan.md`` §7.3 落在阶段 5 的 ``tests/integration/test_api_contract.py``（T-38）——用例层
不带单测。

本用例只做两件事：

1. **单事务快照内定位并判定**：``REPEATABLE READ`` 只读会话里按 ``design.md`` §2.1 落点表的
   表序读三张表（待审表 → 拒审表 → 邮件表），命中的行交给 ``core/states.py`` 的
   :func:`determine_state` —— **全项目唯一的判定落点**（``plan.md`` §4.2），本层不重复判定。
   三次读必须在同一快照内：跨表搬运是事务性的，READ COMMITTED 下每条语句各取一次快照，
   会读到「待审行已删、目标行未可见」的中间组合，把一封正常邮件误报成 ``found: false``
   （``design.md`` §4.1）。
2. **转成契约响应**：命中即 ``found: true`` + 判定出的状态 + 该行的三个时间列；未命中即
   ``found: false`` + 六个字段齐备但状态与时间均为 ``null``（「ID 不存在」不是错误，
   ``design.md`` §4.1、``AC-35``）。ORM 对象到此为止，不出本层（``design.md`` §8）。

**ID 空间只含出站邮件**：契约 ``MessageId`` 是「出站邮件的对外 ID」（``FR-10`` 同样只说按
出站邮件 ID 查），而 ``MessageState`` 六态不表达入站行的终态 ``received``
（``design.md`` §2.3「``received`` 不属于出站状态机」）。入站行 id（对外表现为 ``Reply.id``）
因此不在本端点的 ID 空间内，一律 ``found: false``——这是契约能表达的唯一答案：回
``received`` 不在枚举里、回 ``found: true`` + ``status: null`` 又与该字段「``found`` 为
``false`` 时才为 ``null``」的口径矛盾。

本模块同时是**出站邮件事实读取的唯一落点**：``query_replies.py`` 复用
:func:`read_outbound_message`（存在性）与 :data:`REPEATABLE_READ`——三次读的优先级与方向
判据若各写一份，早晚漂移成两套答案。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from vs_email_forwarding.constants import DIRECTIONS, RECEIVED_STATE, Direction, MessageState
from vs_email_forwarding.core.states import RowFacts, determine_state
from vs_email_forwarding.db.repositories.messages import message_row
from vs_email_forwarding.db.repositories.pending import pending_row
from vs_email_forwarding.db.repositories.rejected import rejected_row
from vs_email_forwarding.db.session import session_factory
from vs_email_forwarding.schemas.messages import MessageStatusResponse

__all__ = [
    "REPEATABLE_READ",
    "OutboundMessageFacts",
    "query_message",
    "read_outbound_message",
]

#: 只读查询会话的事务隔离级别：六态判定要在**同一快照**内读三张表（``design.md`` §4.1
#: 「三次查询同一快照」）。PostgreSQL 默认的 READ COMMITTED 下每条语句各取一次快照，只有
#: 提升到 REPEATABLE READ 才真的落在同一个快照上；按次覆盖由 ``db/session.py`` 的
#: ``session_factory(isolation_level=...)`` 支持（其 docstring 写明只读路径专用）。
REPEATABLE_READ: Final = "REPEATABLE READ"

#: 本端点的 ID 空间方向：出站邮件（``constants.DIRECTIONS`` 即 ``(outbound, inbound)``）。
_OUTBOUND: Final[Direction] = DIRECTIONS[0]


@dataclass(frozen=True, kw_only=True)
class OutboundMessageFacts:
    """单个事务快照内查得的一封出站邮件的事实（``design.md`` §8 的中性对象）。

    这是 ORM 行在 ``service/`` 内部的替身：``state`` 已判定，三个时间列的语义见
    ``design.md`` §2.5（``created_at`` 是 ``NFR-8`` 保留期起算点；``sent_at`` /
    ``reviewed_at`` 表示「某事件是否发生过」，没发生即 ``None``）。
    """

    state: MessageState
    created_at: datetime
    sent_at: datetime | None
    reviewed_at: datetime | None


def _judge(facts: RowFacts) -> MessageState:
    """调用唯一判定落点并把结果收窄到**出站六态**。

    ``received`` 在这里不可达：入站行在 :func:`read_outbound_message` 的方向判据处已被排除。
    真出现即不变量被破坏，宁可失败也不回一个不在 ``MessageState`` 枚举里的值（契约
    ``MessageStatusResponse.status``）。
    """
    state = determine_state(facts)
    if state == RECEIVED_STATE:
        raise ValueError("入站行不属于出站状态机（design.md §2.3）")
    return state


async def read_outbound_message(
    session: AsyncSession, message_id: uuid.UUID
) -> OutboundMessageFacts | None:
    """三次读同一快照，定位该 ID 的出站邮件并判定状态；未命中返回 ``None``。

    读取顺序即 ``design.md`` §2.1 落点表的表序（待审表 → 拒审表 → 邮件表）：同一快照里一行
    至多出现在一张表，按此顺序读即「三次查询同一快照」的落地（``design.md`` §4.1），搬运中的
    行不会两边都查不到。待审表与拒审表都没有 ``sent_at`` 列，`design.md` §2.5：该时刻表示
    「投递被接受」，这两支的行还没过流②，所以恒为 ``None``；拒审行的 ``reviewed_at`` 是审查
    结束时刻。只读、不加锁、不写库；方向判据见模块 docstring（入站行按未命中处理）。
    """
    # 表名 = db/models.py 里对应模型的 __tablename__（RowFacts.table 的契约见 core/states.py）。
    pending = await pending_row(session, message_id)
    if pending is not None and pending.direction == _OUTBOUND:
        # 待审行：唯一的判定输入是 attempt_count（0 = submitted、> 0 = reviewing）。
        return OutboundMessageFacts(
            state=_judge(
                RowFacts(
                    table="pending_messages",
                    direction=_OUTBOUND,
                    attempt_count=pending.attempt_count,
                )
            ),
            created_at=pending.created_at,
            sent_at=None,
            reviewed_at=None,
        )

    rejected = await rejected_row(session, message_id)
    if rejected is not None and rejected.direction == _OUTBOUND:
        # 拒审表没有状态列：行在这张表里本身就是 rejected。
        return OutboundMessageFacts(
            state=_judge(RowFacts(table="rejected_messages", direction=_OUTBOUND)),
            created_at=rejected.created_at,
            sent_at=None,
            reviewed_at=rejected.reviewed_at,
        )

    message = await message_row(session, message_id)
    if message is not None and message.direction == _OUTBOUND:
        # 邮件表：判定依据是 status 列（design.md §2.3 已否决「由 sent_at 推断状态」）。
        return OutboundMessageFacts(
            state=_judge(RowFacts(table="messages", direction=_OUTBOUND, status=message.status)),
            created_at=message.created_at,
            sent_at=message.sent_at,
            reviewed_at=message.reviewed_at,
        )

    return None


async def query_message(message_id: uuid.UUID) -> MessageStatusResponse:
    """查询一封出站邮件的当前状态（``GET /v1/messages/{id}`` 的用例）。

    每次调用开一个 ``REPEATABLE READ`` 只读会话，退出即关（``design.md`` §8：事务边界在
    ``service/``，``api/`` 递不进会话工厂也不该依赖 ``db/``）。会话内只有三次读、没有写，
    因此不需要提交。

    ``message_id`` 已经是 ``api/`` 解析过的 UUID；路径参数不是合法 UUID 的请求根本到不了
    这里（契约把它归为 422，``design.md`` §4.1）。
    """
    factory = session_factory(isolation_level=REPEATABLE_READ)
    async with factory() as session:
        facts = await read_outbound_message(session, message_id)

    if facts is None:
        # 「该 ID 不存在」：成功响应 + found: false，状态与三个时间列同为 null（AC-35）。
        return MessageStatusResponse(
            id=message_id,
            found=False,
            status=None,
            created_at=None,
            sent_at=None,
            reviewed_at=None,
        )

    return MessageStatusResponse(
        id=message_id,
        found=True,
        status=facts.state,
        created_at=facts.created_at,
        sent_at=facts.sent_at,
        reviewed_at=facts.reviewed_at,
    )
