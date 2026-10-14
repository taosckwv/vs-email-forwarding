#!/usr/bin/env python3
"""入站幂等入库仓储（spec 0001 / T-12，`FR-7` / `AC-11`）。

两处入站唯一键（真源：`design.md` §2.2 索引表、§3.4 流③ 落库、§3.5 流④ 落库）：

* ``pending_messages`` 与 ``messages`` 各有一份 ``(mailbox, uidvalidity, uid)``
  **部分唯一索引**（``WHERE direction = 'inbound'``）。两处都建的语义：
  重复的入站邮件在流③ 落待审行时即被挡住；万一漏过（例：上次过审删行后
  游标回退又重拉出一行待审），流④ 写 ``received`` 行时仍被 ``messages``
  侧的键兜住——同一封回复在两张表里都只有一条。
* **幂等插入**：``pg_insert(...).on_conflict_do_nothing(index_elements=[三列键],
  index_where=<direction = inbound>)``——推断部分唯一索引必须携带同一 WHERE
  谓词；撞键后 ``rowcount = 0`` 一律按「这一封已处理过」幂等忽略，返回
  ``False``，不依赖异常文本匹配（`plan.md` §2.9）。跨表搬运 ID 同值的重放
  三列必同值，故冲突目标只需声明三列入站键，主键重放场景已被它覆盖。
* 取时一律用流程层绑定进来的 ``now`` 参数，不用 ``func.now()``（`plan.md` §5.2）：
  ``created_at`` 是该回复的落库时刻（§2.5），流④ 搬运时**原样搬运**、不得重新
  取 now 覆盖；``received_at`` / ``reviewed_at`` 在写回复行时取绑定的 now。
* **过审落库是三态**（``approve_inbound_pending`` 的返回值）：写 ``received`` 行成功 /
  待审行不存在 / **父行已被保留期清理**。第三态按 `design.md` §3.5 的 ``reply_target_gone``
  与 §6「分拣与丢弃」处理——**同事务删除待审行、不写任何目标行**（既不写 ``messages``，
  也不写 ``rejected_messages``），「丢弃」的计数留给流④。留下待审行不行：它会被反复认领，
  第 4 次由收割支写进 ``rejected_messages``，正是 §3.6 要避免的「把用户的**正常回复误报成
  拒审**」。父行是否存在由本层在**同一事务**内用一次 ``SELECT`` 判定：PG 细节（SQLSTATE /
  异常文本 / savepoint）一概不出本层，流层不需要捕获 ``IntegrityError``。代价登记
  （`design.md` §3.6）：判定与插入之间仍有理论窗口——清理流恰好先锁走父行时，窗口内的插入
  会撞外键、由流层的结果事务按 ``SQLAlchemyError`` 吞掉（与流① 同口径）；§3.6 的
  ``NOT EXISTS`` 规则（父行仍有入站待审行时不删）已把该窗口压到极小。

本模块只含语句构造与执行，不承载业务判定（分拣去重计数属流③ 编排，T-25）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, cast

import sqlalchemy as sa
from sqlalchemy import CursorResult
from sqlalchemy.dialects.postgresql import Insert as PGInsert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from vs_email_forwarding.constants import DIRECTIONS, RECEIVED_STATE
from vs_email_forwarding.db.models import Message, PendingMessage

# 受控取值一律从 constants 引用：入站方向取集合第二项（(outbound, inbound)）。
_INBOUND: Final = DIRECTIONS[1]

__all__ = [
    "ApproveInboundOutcome",
    "approve_inbound_pending",
    "inbound_pending_insert_statement",
    "inbound_received_insert_statement",
    "insert_inbound_pending",
]


def inbound_pending_insert_statement() -> PGInsert:
    """构造流③ 入库的幂等 INSERT：撞三列入站唯一键按幂等忽略。

    冲突目标是 ``(mailbox, uidvalidity, uid)`` 部分唯一索引，推断部分索引
    必须携带同一 ``WHERE direction = 'inbound'`` 谓词（`design.md` §2.2）。
    """
    return pg_insert(PendingMessage).on_conflict_do_nothing(
        index_elements=[PendingMessage.mailbox, PendingMessage.uidvalidity, PendingMessage.uid],
        index_where=PendingMessage.direction == _INBOUND,
    )


def inbound_received_insert_statement() -> PGInsert:
    """构造流④ 过审落库的幂等 INSERT：撞三列入站唯一键按幂等忽略。

    ``messages`` 侧同键同谓词：过审删行后游标回退重拉的重复由它兜底
    （`design.md` §2.2 说明：两处都建，重复在落待审行时即被拒）。
    """
    return pg_insert(Message).on_conflict_do_nothing(
        index_elements=[Message.mailbox, Message.uidvalidity, Message.uid],
        index_where=Message.direction == _INBOUND,
    )


async def insert_inbound_pending(
    session: AsyncSession,
    *,
    recipient: str,
    subject: str,
    body: str,
    body_format: str,
    parent_id: uuid.UUID,
    mailbox: str,
    uidvalidity: int,
    uid: int,
    now: datetime,
    pending_id: uuid.UUID | None = None,
) -> bool:
    """流③ 入库（写待审表入站行）；撞入站唯一键按幂等忽略。

    返回是否新写入了行：``False`` 即「这一封已处理过」，流③ 据此计入
    ``duplicate``（`AC-39`）。建行即初值：``attempt_count = 0``、
    ``next_attempt_at = now``；``created_at`` 取该回复的落库时刻（§2.5）。
    """
    result = cast(
        CursorResult[Any],
        await session.execute(
            inbound_pending_insert_statement().values(
                id=pending_id or uuid.uuid4(),
                direction=_INBOUND,
                message_id=None,
                recipient=recipient,
                subject=subject,
                body=body,
                body_format=body_format,
                parent_id=parent_id,
                mailbox=mailbox,
                uidvalidity=uidvalidity,
                uid=uid,
                attempt_count=0,
                next_attempt_at=now,
                created_at=now,
            )
        ),
    )
    return result.rowcount > 0


class ApproveInboundOutcome(StrEnum):
    """流④ 过审落库的三态结果（本层内部词汇，不落库、不出现在任何列里）。"""

    #: 已收尾：写下 ``received`` 行；或撞 ``messages`` 侧入站唯一键（这一封早已入库过）
    #: 而只删待审行。两条路径都表示「回复已在库里」，流层按过审计数（`design.md` §3.5）。
    APPROVED = "approved"
    #: 待审行不存在（已被其他执行体收尾）：不写任何行，按幂等忽略。
    PENDING_GONE = "pending_gone"
    #: 父行已被保留期清理：同事务删掉这条待审行、**不写任何目标行**，流层按
    #: ``reply_target_gone`` 计数丢弃（`design.md` §3.5 / §6）。
    PARENT_GONE = "parent_gone"


async def _parent_row_exists(session: AsyncSession, parent_id: uuid.UUID) -> bool:
    """父行（``parent_id`` 指向的出站邮件）是否仍在：同事务一次 ``SELECT``。

    过审插入会撞 ``fk_messages_parent_id``（``messages.parent_id`` 是自引用外键），
    但捕获外键违例要 savepoint + 认 SQLSTATE，属不该出 ``db/`` 的知识面——改为**先判**。
    判定与插入之间仍有理论窗口（清理流恰好在这一瞬删掉父行，见模块 docstring 的代价登记）。
    """
    stmt = sa.select(Message.id).where(Message.id == parent_id)
    return (await session.execute(stmt)).first() is not None


async def approve_inbound_pending(
    session: AsyncSession, pending_id: uuid.UUID, *, now: datetime
) -> ApproveInboundOutcome:
    """流④ 过审落库：写 ``messages`` 一行 ``received`` + 删待审行；三态见返回值类型。

    同一事务内「写目标行 → 删待审行」；``created_at`` 原样搬运（保持该回复的
    落库时刻），``received_at`` / ``reviewed_at`` 取绑定的 now，``attempt_count``
    与 ``next_attempt_at`` 建行即重置（§2.4）。撞 ``messages`` 侧入站唯一键
    （这一封已入库过）时只删待审行、返回 ``APPROVED``；待审行不存在返回
    ``PENDING_GONE``。

    父行（``parent_id`` 指向的出站邮件）已被保留期清理时返回 ``PARENT_GONE``：
    同一事务内删掉这条待审行，**不写 ``messages``、也不写 ``rejected_messages``**
    ——后者是 ``design.md`` §6 明令否掉的方向（把用户的正常回复误报成拒审）。
    父行判定在同一事务内用一次 ``SELECT`` 完成，流层不接触 PG 错误码与异常文本。

    ``parent_id`` 为空只可能是库外写进来的行（流③ 的 ``insert_inbound_pending``
    该参数必填）：没有父行可判，按既有语义原样插入，不冒充丢弃。
    """
    row = await session.get(PendingMessage, pending_id)
    if row is None:
        return ApproveInboundOutcome.PENDING_GONE
    if row.parent_id is not None and not await _parent_row_exists(session, row.parent_id):
        # 先判后写：父行已清理 → 丢弃（§3.5）。留下待审行会被反复认领并最终误报成拒审（§3.6）。
        await session.execute(sa.delete(PendingMessage).where(PendingMessage.id == pending_id))
        return ApproveInboundOutcome.PARENT_GONE
    await session.execute(
        inbound_received_insert_statement().values(
            id=row.id,
            direction=_INBOUND,
            status=RECEIVED_STATE,
            message_id=None,
            parent_id=row.parent_id,
            recipient=row.recipient,
            subject=row.subject,
            body=row.body,
            body_format=row.body_format,
            mailbox=row.mailbox,
            uidvalidity=row.uidvalidity,
            uid=row.uid,
            attempt_count=0,
            next_attempt_at=now,
            created_at=row.created_at,
            received_at=now,
            reviewed_at=now,
        )
    )
    await session.execute(sa.delete(PendingMessage).where(PendingMessage.id == pending_id))
    return ApproveInboundOutcome.APPROVED
