#!/usr/bin/env python3
"""``messages`` 仓储（发信侧）：认领与条件 UPDATE（spec 0001 / T-7）。

写法口径（真源：`specs/0001-email-forwarding-service/design.md` §3 流②、
`plan.md` §5.1 / §5.2）：

* **认领（阶段①）**：``select(...).with_for_update(skip_locked=True)``，
  谓词 ``direction = outbound`` 且 ``status = sending`` 且 ``attempt_count <= 4``
  且 ``next_attempt_at <= <绑定的 now 参数>``；认领第 n 次（n < 4）在同一事务内
  推 ``attempt_count`` 与 ``next_attempt_at``（退避表在 constants）；
  ``attempt_count = 4`` 的行是收割支命中面，认领时**不递增、不推退避**
  （不产生第 5 次投递）。两个分支在返回值里逐行给出分流标志
  （``(row, is_harvest)``）——认领之后两种行的列值完全一样，调用方只能靠这一位
  分辨谁要真的投递、谁要直接置终态。
* **结果（阶段③）**：条件 UPDATE 只允许 ``design.md`` §2.3 声明的三个起点——
  ``sending → sent``、``sending → failed``、``sent → failed``（退信覆写）；
  谓词把起点写进 ``WHERE``，``rowcount = 0`` 按幂等忽略、返回 ``False``。
* **只读整行（同步查询路径，T-29 前置）**：``message_row`` 与 ``replies_page`` 按 ID
  交出**整行**——六态判定要 ``direction``，响应模型要三个时间列，``Reply`` 契约要
  内容列，单列投影给不了（``design.md`` §4.1 / §8、``plan.md`` §2.9）。只读、不加锁，
  行不存在返回 ``None``、无回复返回空页。
* 取时一律用流程层绑定进来的 ``now`` 参数，不用 ``func.now()``（``plan.md`` §5.2）。

本模块只含语句构造与执行，不承载业务判定（返回码分类属 ``core/retry.py``）。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Final, cast

import sqlalchemy as sa
from sqlalchemy import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from vs_email_forwarding.constants import (
    DIRECTIONS,
    MAX_ATTEMPTS,
    MAX_CLAIMABLE_ATTEMPT_COUNT,
    MESSAGE_STATES,
    RETRY_BACKOFF_SECONDS,
)
from vs_email_forwarding.db.models import Message, PendingMessage

# 受控取值一律从 constants 引用，不在本模块重复字面量：
# 方向取集合首项（DIRECTIONS 即 (outbound, inbound)）；
# 出站三态依次是六态的第 4 ~ 6 项（sending / sent / failed）。
_OUTBOUND: Final = DIRECTIONS[0]
_SENDING: Final = MESSAGE_STATES[3]
_SENT: Final = MESSAGE_STATES[4]
_FAILED: Final = MESSAGE_STATES[5]

#: 一次认领返回的一行与它的分流标志。``is_harvest`` 为 ``True`` 表示这一行是
#: 「第 4 次尝试已认领、结果事务未提交」的收割遗留行（本次认领**未**递增计数）：
#: 调用方不得对它发起 SMTP 投递，直接置终态 ``failed``（``design.md`` §3 流② /
#: §2.4、``plan.md`` §5.3）。为 ``False`` 的是本次真正认领到的尝试（含真·第 4 次
#: 尝试），必须发起投递。
type ClaimedSendMessage = tuple[Message, bool]

#: 出站行的关联目标：``(主键, sent_at)``。中性二元组——回复支只取主键当
#: ``parent_id``（``design.md`` §2.2 的关联口径）；退信支再看 ``sent_at`` 是否为空，
#: 区分「曾被 MTA 接受」（置 ``failed``）与「尚未发出」（``dsn_target_not_sent``，
#: ``design.md`` §3.3「退信到达」）。本层不替调用方作这三种判定。
type OutboundTarget = tuple[uuid.UUID, datetime | None]

__all__ = [
    "ClaimedSendMessage",
    "OutboundTarget",
    "claim_due_send_messages",
    "claim_due_send_statement",
    "mark_failed_from_sent",
    "mark_send_failed",
    "mark_sent",
    "message_row",
    "message_status",
    "outbound_targets_by_message_id",
    "pending_attempt_count",
    "replies_page",
]


def claim_due_send_statement(*, now: datetime, batch_size: int) -> Select:
    """构造发信认领 SELECT：锁行 + 谓词 + 到点排序 + 分批上限。

    谓词即流② 的认领条件（``design.md`` §3 流②）：出站、``status = sending``
    （显式等待态）、``attempt_count <= 4``（放宽到 4 是收割支的命中面）、
    ``next_attempt_at <= now``。``FOR UPDATE SKIP LOCKED`` 保证并发执行体
    各抢各的行（``AC-29`` 的库内侧）。
    """
    return (
        sa.select(Message)
        .where(
            Message.direction == _OUTBOUND,
            Message.status == _SENDING,
            Message.attempt_count <= MAX_CLAIMABLE_ATTEMPT_COUNT,
            Message.next_attempt_at <= now,
        )
        .order_by(Message.next_attempt_at)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )


async def claim_due_send_messages(
    session: AsyncSession, *, now: datetime, batch_size: int
) -> list[ClaimedSendMessage]:
    """认领一批到点的待发行：锁行、推进尝试计数与退避，提交由调用方负责。

    认领第 n 次（n < 4）把 ``attempt_count`` 推到 n 并按退避表推 ``next_attempt_at``
    （第 4 次不推——该行只剩终态一条路），返回 ``(row, False)``；
    ``attempt_count = 4`` 的行原样返回，返回 ``(row, True)``：它是「第 4 次尝试已认领、
    结果事务未提交」的收割遗留行，调用方**不得**发起投递，直接置终态 ``failed``
    （`plan.md` §5.2 / §5.3）。锁与推进在同一事务内完成，不是「先 SELECT 判断、
    再 UPDATE」的两步写法。

    分流标志只能在这一层给出：认领之后两种行的 ``attempt_count`` 都是 4、
    ``next_attempt_at`` 也都是认领前那个值，出库后无法再分辨；而流② 对两者的处置
    相反（真·第 4 次尝试必须真的投递，收割遗留行必须不产生第 5 次投递，
    `AC-8` / `AC-37`）。
    """
    rows = list(
        (await session.scalars(claim_due_send_statement(now=now, batch_size=batch_size))).all()
    )
    claimed: list[ClaimedSendMessage] = []
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


async def mark_sent(session: AsyncSession, message_id: uuid.UUID, *, now: datetime) -> bool:
    """条件 UPDATE ``sending → sent``：写 ``sent_at``；rowcount = 0 按幂等忽略。

    谓词把声明的起点写进 ``WHERE``：起点已不是 ``sending``（例：结果重复提交）
    时不写任何行，返回 ``False``，不报错（`plan.md` §5.2）。
    """
    result = cast(
        CursorResult[Any],
        await session.execute(
            sa.update(Message)
            .where(Message.id == message_id, Message.status == _SENDING)
            .values(status=_SENT, sent_at=now)
        ),
    )
    return result.rowcount > 0


async def mark_send_failed(
    session: AsyncSession,
    message_id: uuid.UUID,
    *,
    error_class: str,
    smtp_code: int | None,
    now: datetime,
) -> bool:
    """条件 UPDATE ``sending → failed``：记录最后一次错误的分类与 SMTP 返回码。

    适用于投递失败置终态（含 ``5xx`` 直接终态与收割支推进——第 4 次认领的行
    仍处于 ``sending``，本语句就是它的终态出口）。起点不是 ``sending`` 时不写行。
    ``smtp_code`` 为 ``None`` 表示非 SMTP 故障（如连接超时、收割支无返回码）。
    """
    result = cast(
        CursorResult[Any],
        await session.execute(
            sa.update(Message)
            .where(Message.id == message_id, Message.status == _SENDING)
            .values(status=_FAILED, last_error_class=error_class, last_smtp_code=smtp_code)
        ),
    )
    return result.rowcount > 0


async def mark_failed_from_sent(
    session: AsyncSession,
    message_id: uuid.UUID,
    *,
    error_class: str,
    now: datetime,
) -> bool:
    """条件 UPDATE ``sent → failed``：退信（DSN）把已发出的行覆写为终态失败。

    这是 ``design.md`` §2.3 声明的第三个迁移；它**不写** ``sent_at``（已发出的
    事实不因退信而消失），也不碰 ``attempt_count`` / ``next_attempt_at``
    （结果事务一律不碰这两列，`plan.md` §5.2）。
    """
    result = cast(
        CursorResult[Any],
        await session.execute(
            sa.update(Message)
            .where(Message.id == message_id, Message.status == _SENT)
            .values(status=_FAILED, last_error_class=error_class)
        ),
    )
    return result.rowcount > 0


# ---------------------------------------------------------------------------
# 查询侧（spec 0001 / T-11）：六态判定的库内输入与回复 keyset 分页
# （design §4.1：排序固定 received_at ASC, id ASC；不用 limit/offset）
# ---------------------------------------------------------------------------


async def message_row(session: AsyncSession, message_id: uuid.UUID) -> Message | None:
    """按 ID 读一整行 ``messages`` 行；行不存在返回 ``None``（``T-29`` 前置）。

    同步查询路径的读入口（``design.md`` §4.1 / §8、``plan.md`` §2.5 / §2.9）：六态判定
    需要 ``direction``（``RowFacts`` 的必填字段，``core/states.py``），响应模型还需要
    ``status``、``created_at`` / ``sent_at`` / ``reviewed_at``——这些都不是
    :func:`message_status` 的单列投影能给的。整行交给 ``service/``，由它在自己的会话
    有效期内立即转成 ``schemas/`` 的响应模型或中性对象（``design.md`` §8；
    ``db/session.py`` 的 ``expire_on_commit=False`` 正是为此预备）。

    **只读**：一条 ``SELECT``、无 ``FOR UPDATE``、不写库、不取时（无 ``now`` 参数）。
    行不存在＝「该 ID 不存在」，是正常结果而不是错误（``design.md`` §4.1：两个查询端点
    对不存在的 ID 都返回成功状态）。
    """
    stmt = sa.select(Message).where(Message.id == message_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def message_status(session: AsyncSession, message_id: uuid.UUID) -> str | None:
    """读 ``messages.status`` 列值；行不存在返回 ``None``。

    六态中的 ``sending`` / ``sent`` / ``failed`` 由该列值直接表达；
    ``submitted`` / ``reviewing`` / ``rejected`` 的判定输入分别由
    :func:`pending_attempt_count` 与 ``rejected_messages`` 的存在性查询提供，
    优先级判定属 ``core``（``design.md`` §2.3），不在本层。
    """
    row = (await session.execute(sa.select(Message.status).where(Message.id == message_id))).first()
    return None if row is None else row[0]


async def pending_attempt_count(session: AsyncSession, pending_id: uuid.UUID) -> int | None:
    """读待审行的 ``attempt_count``；行不存在返回 ``None``。

    ``0`` 与 ``> 0`` 分别是「已提交」与「审查中」的库内输入（``design.md`` §2.3）。
    """
    row = (
        await session.execute(
            sa.select(PendingMessage.attempt_count).where(PendingMessage.id == pending_id)
        )
    ).first()
    return None if row is None else row[0]


async def replies_page(
    session: AsyncSession,
    parent_id: uuid.UUID,
    *,
    after: tuple[datetime | None, uuid.UUID | None],
    limit: int,
) -> list[Message]:
    """取某出站邮件的一页回复（**整行**）：keyset 游标 ``received_at ASC, id ASC``。

    ``after`` 是上一页最后一行的 ``(received_at, id)``（首页传 ``(None, None)``）；
    排序键与 ``(parent_id, received_at)`` 索引对齐，翻页用行值比较而非
    ``limit`` / ``offset``——新回复到达不会让偏移窗口漂移（``AC-41``）。

    交出整行是查询用例的要求（``T-29`` 前置）：``Reply`` 契约要 ``subject`` /
    ``body`` / ``body_format``（``docs/api-contract.yaml``、``design.md`` §4.1），
    只取 ``(id, received_at)`` 时内容列拿不到；下一页的游标由调用方从返回行的
    ``received_at`` / ``id`` 拼。**只读**：一条 ``SELECT``、无锁、不写库。
    """
    after_received_at, after_id = after
    stmt = sa.select(Message).where(Message.parent_id == parent_id)
    if after_received_at is not None and after_id is not None:
        stmt = stmt.where(
            sa.tuple_(Message.received_at, Message.id) > sa.tuple_(after_received_at, after_id)
        )
    stmt = stmt.order_by(Message.received_at, Message.id).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# T-25 前置（spec 0001）：按 Message-ID 读出站行
# （流③ 回复支与 DSN 退信支共用；design §2.2 的 message_id 列口径）
# ---------------------------------------------------------------------------


async def outbound_targets_by_message_id(
    session: AsyncSession, message_ids: Sequence[str]
) -> dict[str, OutboundTarget]:
    """按 Message-ID 批量读出站行，返回 ``Message-ID → (主键, sent_at)``。

    流③ 两条支路的共用入口（``plan.md`` §5.4）：**回复支**用返回的键当「已知集合」
    （命中即说明引用指向本服务的出站邮件）、用主键当回复行的 ``parent_id``；
    **退信支**用返回的 ``sent_at`` 分辨「曾被 MTA 接受」（非空 → 置 ``failed``）
    与「尚未发出」（空 → ``dsn_target_not_sent``、不打断重试）。这两种判定属调用方，
    本层只把库内事实读出来。

    入参是候选标识集合，未命中的标识**不出现**在结果里（缺键＝该 Message-ID 没有
    对应行，例：指向的邮件已被保留期清理，``design.md`` §3.3 的 ``dsn_target_gone``）。
    匹配是精确串比较：解析、去空白与大小写规范化属 ``core/inbound``，不在本层。
    ``message_id`` 上建的是全局唯一索引（``design.md`` §2.2），故每个键至多一行。
    只读出站行、不写库、不取时（无 ``now`` 参数）。
    """
    rows = (
        await session.execute(
            sa.select(Message.message_id, Message.id, Message.sent_at).where(
                Message.direction == _OUTBOUND,
                Message.message_id.in_(message_ids),
            )
        )
    ).all()
    # 出站行的 message_id 由 CHECK 保证非空（models.py ck_messages_outbound_required）；
    # 列在 ORM 上声明为可空，故在此收窄类型。
    return {cast(str, row.message_id): (row.id, row.sent_at) for row in rows}
