#!/usr/bin/env python3
"""``pending_messages`` 仓储：三阶段认领与条件写入（spec 0001 / T-6）。

写法口径（真源：`specs/0001-email-forwarding-service/design.md` §3、
`plan.md` §2.9 / §5.1 ~ §5.3）：

* **建行（提交路径，同步请求）**：``insert_outbound_pending`` 写一行出站待审行
  （``direction = outbound``、``message_id`` 非空、入站三要素留空、``attempt_count = 0``、
  ``next_attempt_at`` 与 ``created_at`` 都取入参 ``now``），供流① 认领（``design.md`` §3.1）。
* **认领（阶段①）**：``select(...).with_for_update(skip_locked=True)``，
  谓词 ``attempt_count <= 4`` 且 ``next_attempt_at <= <绑定的 now 参数>``；
  认领第 n 次（n < 4）在同一事务内把 ``attempt_count`` 推到 n、
  ``next_attempt_at`` 推到 ``now + 退避[n]``；``attempt_count = 4`` 的行
  是收割支的命中面，认领时**不递增、不推退避**（不产生第 5 次尝试）。
  两个分支在返回值里逐行给出分流标志（``(row, is_harvest)``）——认领之后两种行的
  列值完全一样，调用方只能靠这一位分辨谁要发起外部调用、谁要直接推进终态。
* **结果（阶段③）**：过审 → ``messages`` 一行 ``sending``（跨表 ID 同值、
  ``attempt_count`` 重置为 0、``next_attempt_at`` 重置为参数绑定的 now）；
  拒审 / 收割 → ``rejected_messages`` 一行（``source`` 由调用方给：
  ``review_rejected`` / ``review_error`` / ``review_incomplete``）。
  两个目标语句都带 ``ON CONFLICT DO NOTHING``——撞键按「这一封已处理过」幂等忽略，
  之后一律删待审行。
* 取时一律用流程层绑定进来的 ``now`` 参数，不用 ``func.now()``（``plan.md`` §5.2）。
* **只读整行（同步查询路径，T-29 前置）**：``pending_row`` 按 ID 交出整行，供查询用例
  做六态判定（``attempt_count``）与响应组装（``created_at``）——``design.md`` §4.1 / §8、
  ``plan.md`` §2.9。只读、不加锁。

本模块只含语句构造与执行，不承载业务判定（过审与否属审查流）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import Insert as PGInsert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from vs_email_forwarding.constants import (
    DIRECTION_INBOUND,
    DIRECTION_OUTBOUND,
    ERROR_CLASSES,
    MAX_ATTEMPTS,
    MAX_CLAIMABLE_ATTEMPT_COUNT,
    MESSAGE_STATE_SENDING,
    REJECT_SOURCE_REVIEW_INCOMPLETE,
    REJECT_SOURCES,
    RETRY_BACKOFF_SECONDS,
)
from vs_email_forwarding.db.models import Message, PendingMessage, RejectedMessage

# 受控取值一律从 constants 取具名常量，不在本模块重复字面量：
# 出站 / 入站各取方向的名字（`DIRECTIONS` 即 (outbound, inbound)），
# sending 取出站投递三态的具名常量（messages.status 的第一个取值）。
_OUTBOUND: Final = DIRECTION_OUTBOUND
_INBOUND: Final = DIRECTION_INBOUND
_SENDING: Final = MESSAGE_STATE_SENDING

#: 一次认领返回的一行与它的分流标志。``is_harvest`` 为 ``True`` 表示这一行是
#: 「第 4 次尝试已认领、结果事务未提交」的收割遗留行（本次认领**未**递增计数）：
#: 调用方不得对它发起外部调用，直接推进终态（``design.md`` §3 阶段① / §2.4、``plan.md`` §5.3）。
#: 为 ``False`` 的是本次真正认领到的尝试（含真·第 4 次尝试），必须发起外部调用。
type ClaimedPendingMessage = tuple[PendingMessage, bool]

__all__ = [
    "ClaimedPendingMessage",
    "approval_insert_statement",
    "approve_pending_message",
    "claim_due_pending_messages",
    "claim_due_statement",
    "harvest_pending_message",
    "insert_outbound_pending",
    "pending_row",
    "reject_pending_message",
]


async def insert_outbound_pending(
    session: AsyncSession,
    *,
    pending_id: uuid.UUID,
    message_id: str,
    recipient: str,
    subject: str,
    body: str,
    body_format: str,
    now: datetime,
) -> None:
    """提交路径建行：写一行出站待审行，供流① 认领（``design.md`` §3.1）。

    ``pending_id`` 与 ``message_id`` 由提交方传入：``pending_id`` 既是对外 ID、
    也是主键（§3.1「``id`` 与主键同值」），``message_id`` 由 ``core/message_id``
    按该 ``id`` 与发件域生成（同节）——本函数不做任何生成、解析或业务判定。

    建行初值：``attempt_count = 0``、``next_attempt_at`` 与 ``created_at`` **都取入参
    ``now``**。取时只有用例层一处（``plan.md`` §5.2），不用 ``func.now()`` 兜底；
    两个时间列必须等于提交时刻——``created_at`` 是 ``NFR-8`` 30 天保留期的起算点
    （§2.5），``next_attempt_at`` 建行即可被抢占（§2.4）。

    ``direction = outbound``，且 ``parent_id`` / ``mailbox`` / ``uidvalidity`` / ``uid``
    全部留空：出站行没有 IMAP 来源，这几个字段只属入站行（§2.1 / §2.2 的行模型 CHECK
    兜底）。``body_format`` 由调用方给（取值合法性由 schema 层保证），本层只做搬运。

    **本建行不是幂等路径**：``pending_id`` 每次提交新生成，撞主键只可能是调用方的
    编程错误，因此**不**用 ``ON CONFLICT DO NOTHING`` 把它吞掉——入站侧的
    :func:`insert_inbound_pending` 之所以幂等，是因为那里有 ``(mailbox, uidvalidity, uid)``
    唯一键要兜住游标回退重拉（§3.4），提交路径没有对应的重复来源。返回 ``None``：
    没有「是否新写入」这一位可表达，提交方本来就持有 ``pending_id``。
    """
    await session.execute(
        sa.insert(PendingMessage).values(
            id=pending_id,
            direction=_OUTBOUND,
            message_id=message_id,
            recipient=recipient,
            subject=subject,
            body=body,
            body_format=body_format,
            parent_id=None,
            mailbox=None,
            uidvalidity=None,
            uid=None,
            attempt_count=0,
            next_attempt_at=now,
            created_at=now,
        )
    )


def claim_due_statement(*, direction: str, now: datetime, batch_size: int) -> Select:
    """构造待审行的认领 SELECT：锁行 + 谓词 + 到点排序 + 分批上限。

    谓词即认领条件（``design.md`` §3 阶段①）：方向匹配、``attempt_count <= 4``
    （放宽到 4 是收割支的命中面）、``next_attempt_at <= now``（now 由流程层绑定，
    不用数据库时钟）。``FOR UPDATE SKIP LOCKED`` 保证并发执行体各抢各的行。
    """
    return (
        sa.select(PendingMessage)
        .where(
            PendingMessage.direction == direction,
            PendingMessage.attempt_count <= MAX_CLAIMABLE_ATTEMPT_COUNT,
            PendingMessage.next_attempt_at <= now,
        )
        .order_by(PendingMessage.next_attempt_at)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )


async def claim_due_pending_messages(
    session: AsyncSession, *, direction: str, now: datetime, batch_size: int
) -> list[ClaimedPendingMessage]:
    """认领一批到点的待审行：锁行、推进尝试计数与退避，提交由调用方负责。

    认领第 n 次（n < 4）把 ``attempt_count`` 推到 n 并按退避表推 ``next_attempt_at``
    （第 4 次不推——该行只剩终态一条路），返回 ``(row, False)``；
    ``attempt_count = 4`` 的行原样返回，返回 ``(row, True)``：它是「第 4 次尝试已认领、
    结果事务未提交」的收割遗留行，调用方**不得**对它发起外部调用，直接推进终态
    （`plan.md` §5.2 / §5.3）。锁与推进在同一事务内完成，不是「先 SELECT 判断、
    再 UPDATE」的两步写法。

    分流标志只能在这一层给出：认领之后两种行的 ``attempt_count`` 都是 4、
    ``next_attempt_at`` 也都是认领前那个值，出库后无法再分辨；而流① / 流④ 对两者的
    处置相反（真·第 4 次尝试必须发起审查调用，收割遗留行必须跳过并置
    ``review_incomplete``，`design.md` §3 阶段①）。
    """
    rows = list(
        (
            await session.scalars(
                claim_due_statement(direction=direction, now=now, batch_size=batch_size)
            )
        ).all()
    )
    claimed: list[ClaimedPendingMessage] = []
    for row in rows:
        if row.attempt_count >= MAX_ATTEMPTS:
            # 收割支命中面：不递增、不推退避、不发起任何外部调用（design.md §3）。
            claimed.append((row, True))
            continue
        row.attempt_count += 1
        backoff_seconds = RETRY_BACKOFF_SECONDS.get(row.attempt_count)
        if backoff_seconds is not None:
            row.next_attempt_at = now + timedelta(seconds=backoff_seconds)
        claimed.append((row, False))
    return claimed


def approval_insert_statement() -> PGInsert:
    """构造过审落库的 INSERT：跨表 ID 同值、重置尝试列、撞键幂等忽略。

    ``attempt_count = 0`` 与 ``next_attempt_at = now`` 建行即重置
    （审查重试不侵占发信重试额度，``design.md`` §2.4）；``ON CONFLICT DO NOTHING``
    冲突目标是主键 ``id``（跨表搬运 ID 同值，撞它即「这一封已处理过」，
    ``plan.md`` §2.9 的构造器写法）。语句的取值在执行时经
    :func:`approve_pending_message` 绑定。
    """
    return pg_insert(Message).on_conflict_do_nothing(index_elements=[Message.id])


async def approve_pending_message(
    session: AsyncSession, pending_id: uuid.UUID, *, now: datetime
) -> bool:
    """过审落库（阶段③）：写 ``messages`` 一行 ``sending`` + 删待审行。

    同一事务内「写目标行 → 删待审行」（`design.md` §3 流① 落库口径）；
    ``created_at`` 原样搬运、保持原始提交时刻，``reviewed_at`` 记绑定的 now。
    撞唯一键 / 主键时只删待审行、不重复写目标行，返回 ``True`` 表示待审行已收尾。
    待审行不存在（已被其他执行体收尾）时返回 ``False``，按幂等忽略。
    """
    row = await session.get(PendingMessage, pending_id)
    if row is None:
        return False
    await session.execute(
        approval_insert_statement().values(
            id=row.id,
            direction=row.direction,
            status=_SENDING,
            message_id=row.message_id,
            recipient=row.recipient,
            subject=row.subject,
            body=row.body,
            body_format=row.body_format,
            attempt_count=0,
            next_attempt_at=now,
            created_at=row.created_at,
            reviewed_at=now,
        )
    )
    await session.execute(sa.delete(PendingMessage).where(PendingMessage.id == pending_id))
    return True


async def reject_pending_message(
    session: AsyncSession,
    pending_id: uuid.UUID,
    *,
    source: str,
    now: datetime,
    error_class: str | None = None,
) -> bool:
    """拒审落库（阶段③）：写 ``rejected_messages`` 一行 + 删待审行。

    跨表 ID 同值（架构图的「id 与目标行同值」）；``subject`` / ``body`` 仅入站行搬运，
    出站行保持 NULL（表上的 ``CHECK`` 兜底）。``source`` 只接受三个受控取值。

    ``error_class`` 是**审查出错路径**（``source = review_error``）要落的受控分类：
    审查环节异常即 ``internal``（`design.md` §2.4 / §3 阶段③）。待审行上没有任何路径写
    ``last_error_class``，因此分类由调用方从接缝显式给、不搬运待审行——与
    ``messages.py`` 的 ``mark_send_failed(error_class=…)`` 同口径；判定拒与收割路径
    传 ``None``（不存在「最后一次错误」，`design.md` §2.4 允许该列为空）。
    越界取值在写入前即拒（同 ``source`` 的校验）。
    """
    if source not in REJECT_SOURCES:
        msg = f"unknown reject source: {source}"
        raise ValueError(msg)
    if error_class is not None and error_class not in ERROR_CLASSES:
        msg = f"unknown error class: {error_class}"
        raise ValueError(msg)
    row = await session.get(PendingMessage, pending_id)
    if row is None:
        return False
    await session.execute(
        pg_insert(RejectedMessage)
        .on_conflict_do_nothing(index_elements=[RejectedMessage.id])
        .values(
            id=row.id,
            direction=row.direction,
            source=source,
            subject=row.subject if row.direction == _INBOUND else None,
            body=row.body if row.direction == _INBOUND else None,
            last_error_class=error_class,
            created_at=row.created_at,
            reviewed_at=now,
        )
    )
    await session.execute(sa.delete(PendingMessage).where(PendingMessage.id == pending_id))
    return True


async def harvest_pending_message(
    session: AsyncSession, pending_id: uuid.UUID, *, now: datetime
) -> bool:
    """收割支写入（``plan.md`` §5.3）：第 4 次认领后崩溃的遗留行推进终态。

    等价于 ``source = review_incomplete`` 的拒审落库——不递增计数、不推退避、
    不发起任何外部调用，单独成函数是为了让调用方（审查流）的收割分支
    显式可见、可审计。收割是崩溃窗口、不存在「最后一次错误」，故不传
    ``error_class``（该列保持空）。
    """
    return await reject_pending_message(
        session, pending_id, source=REJECT_SOURCE_REVIEW_INCOMPLETE, now=now
    )


# ---------------------------------------------------------------------------
# T-29 前置：只读整行（同步查询路径）
# （design §4.1 判定表的第一级分流：行在不在待审表里 + attempt_count）
# ---------------------------------------------------------------------------


async def pending_row(session: AsyncSession, pending_id: uuid.UUID) -> PendingMessage | None:
    """按 ID 读一整行待审行；行不存在返回 ``None``（``T-29`` 前置）。

    六态判定按「行在哪张表」分流，待审表这一支的输入是 ``attempt_count``
    （``0`` = ``submitted``、``> 0`` = ``reviewing``，含审查失败的退避等待；
    ``design.md`` §2.3 落点表），响应模型还要 ``created_at``（``NFR-8`` 保留期起算点，
    §2.5）。整行交给 ``service/``，由它就地转换（``design.md`` §8）。

    **只读**：一条 ``SELECT``、无 ``FOR UPDATE``、不写库、不取时。行不存在是正常结果
    （该 ID 不在待审表里，判定继续往拒审表 / 邮件表走）。
    """
    stmt = sa.select(PendingMessage).where(PendingMessage.id == pending_id)
    return (await session.execute(stmt)).scalar_one_or_none()
