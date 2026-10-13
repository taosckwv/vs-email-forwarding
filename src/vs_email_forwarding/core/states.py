#!/usr/bin/env python3
"""六态判定的唯一纯函数落点（`plan.md` §4.2：全项目只此一处，`service/` 与
`api/` 只准调用、不得各自再实现）。

输入是**行的中性事实**——行在哪张表、``direction``、``messages`` 行的
``status`` 列值、待审行的 ``attempt_count``，全部由调用方在**单个事务快照**
内查得后传入（`design.md` §4.1：跨表搬运是事务性的，同一快照里一行至多
出现在一张表）。本模块无 I/O、不读数据库与配置。

``sent_at`` **不进判定输入**：`design.md` §2.3 已明确否决「由 ``sent_at``
推断状态」——``sent_at IS NULL`` 同时涵盖 ``submitted`` / ``reviewing`` 与
从未被接受的 ``failed``，``status`` 列才是唯一判据。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from vs_email_forwarding.constants import (
    MESSAGE_STATES,
    MESSAGES_STATUS_VALUES,
    RECEIVED_STATE,
    Direction,
    MessageState,
)

__all__ = ["STATE_LOCATIONS", "JudgedState", "RowFacts", "determine_state"]

#: 承载邮件行的三张物理表（`design.md` §2；`imap_cursors` 不承载邮件行）。
_MAIL_TABLES: Final[tuple[str, ...]] = ("pending_messages", "messages", "rejected_messages")

# 判定函数的全部可能输出：出站六态 + 入站终态（`design.md` §2.1 判定表 7 行）。
# constants 只以**值**形式导出 RECEIVED_STATE（不成类型别名），类型级并集只能
# 在本地以同一字面量补齐；运行期取值仍一律引用 constants.RECEIVED_STATE。
type JudgedState = MessageState | Literal["received"]

#: 出站行在 `messages.status` 上的合法列值（六态中同时是列值的三态；由
#: constants 推出，不另抄一份词表）。
_OUTBOUND_COLUMN_STATES: Final[tuple[MessageState, ...]] = tuple(
    state for state in MESSAGE_STATES if state in MESSAGES_STATUS_VALUES
)

#: 每个判定输出的物理落点（`design.md` §2.1 落点表的文字结论）。供对外文档
#: 与排查使用；判定本身不读它。
STATE_LOCATIONS: Final[dict[JudgedState, str]] = {
    "submitted": "pending_messages（attempt_count = 0，尚未被认领）",
    "reviewing": "pending_messages（attempt_count > 0，含审查失败的退避等待）",
    "rejected": "rejected_messages（source 列区分来源，不参与判定）",
    "sending": "messages.status（出站行初始态，涵盖首次待发与重试等待）",
    "sent": "messages.status（出站行，投递已被接受）",
    "failed": "messages.status（出站行终态失败；退信到达后自 sent 迁入）",
    RECEIVED_STATE: "messages.status（入站回复行，写入即终态）",
}


@dataclass(frozen=True, kw_only=True)
class RowFacts:
    """一次六态判定的中性输入事实（调用方在单个事务快照内查得）。"""

    #: 行当前所在的物理表——判定的第一级分流（取值见 `_MAIL_TABLES`，
    #: 非法值由 :func:`determine_state` 拒绝）。
    table: str

    #: 行方向：`messages` 行据此分流入站终态与出站三态。
    direction: Direction

    #: `messages` 行的 `status` 列值；其余两表没有该列，保持 ``None``。
    status: str | None = None

    #: 待审行的认领次数；建行即 0、只会递增（`design.md` §2.4）。
    attempt_count: int = 0


def determine_state(facts: RowFacts) -> JudgedState:
    """按 `design.md` §2.1 落点表的优先级自上而下判定该行的对外状态。

    分流顺序即落点表行序：待审表（``submitted`` / ``reviewing``）→ 拒审表
    （``rejected``）→ 邮件表（出站行列值三态 / 入站行终态 ``received``）。
    库内与对外是同一套取值，本函数**不做任何状态翻译**（`plan.md` §4.2：
    没有映射层）。不可能组合（非邮件表、负的认领次数、入站行非
    ``received``、出站行 ``received``、未知列值）对应库内 ``CHECK`` 或表
    模型，出现在输入里即调用方构造错误，直接抛 :class:`ValueError`，不猜。
    """
    if facts.table not in _MAIL_TABLES:
        raise ValueError(f"不承载邮件行的表：{facts.table}")
    if facts.table == "pending_messages":
        if facts.attempt_count < 0:
            raise ValueError(f"attempt_count 不能为负：{facts.attempt_count}")
        # 落点表：= 0 → submitted；> 0 → reviewing（含审查失败的退避等待）。
        return "reviewing" if facts.attempt_count > 0 else "submitted"
    if facts.table == "rejected_messages":
        # 拒审表不设状态列：行在这张表里本身就是 rejected。
        return "rejected"
    if facts.direction == "inbound":
        # 行模型 CHECK：入站行只能 received，写入即终态、不迁移。
        if facts.status != RECEIVED_STATE:
            raise ValueError(f"入站行只可能是 received：{facts.status}")
        return RECEIVED_STATE
    for column_state in _OUTBOUND_COLUMN_STATES:
        if facts.status == column_state:
            return column_state
    raise ValueError(f"出站行 status 非法：{facts.status}")
