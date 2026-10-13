#!/usr/bin/env python3
"""六态判定（`core/states.py`）的纯逻辑单测。

覆盖：判定表每一行的正例（`AC-36` 的判定基础——对外状态的单调推进由这张
判定表唯一决定）、`attempt_count` 与 `direction` 两处分流的边界两侧、不可能
组合的拒绝（对应库内 `CHECK` 与表模型）、非邮件表的拒绝，以及物理落点映射
对判定词表的全覆盖。

期望值来源（`docs/testing.md` R2，全部为规格推导的字面量）：

* 每组事实对应的六态取值：`design.md` §2.1 落点表（受控枚举取值表逐行：
  `submitted` = 待审行且 `attempt_count = 0`、`reviewing` = 待审行且
  `attempt_count > 0`、`rejected` = 拒审表、`sending` / `sent` / `failed` =
  `messages` 出站行列值、`received` = `messages` 入站行终态）；
* 不可能组合：`design.md` §2.2 的行模型 `CHECK`（入站行只能 `received`、
  出站行不得为 `received`）与 §1（`imap_cursors` 不承载邮件行）；
* 判定词表全集：`vs_email_forwarding.constants`（六态 + 入站终态）。
"""

from __future__ import annotations

import pytest

from vs_email_forwarding import constants
from vs_email_forwarding.core.states import STATE_LOCATIONS, RowFacts, determine_state


def test_pending_row_unclaimed_is_submitted() -> None:
    # design.md §2.1：submitted = 待审行且 attempt_count = 0（尚未被认领过）。
    assert determine_state(RowFacts(table="pending_messages", direction="outbound")) == "submitted"


def test_pending_row_claimed_is_reviewing() -> None:
    # design.md §2.1：reviewing = 待审行且 attempt_count > 0（边界另一侧：
    # 首次认领后即为 1；含审查失败的退避等待）。
    assert (
        determine_state(RowFacts(table="pending_messages", direction="outbound", attempt_count=1))
        == "reviewing"
    )


def test_rejected_row_is_rejected() -> None:
    # design.md §2.1：rejected 的落点是 rejected_messages；source 只作库内
    # 区分（review_rejected / review_error / review_incomplete），不参与判定。
    assert determine_state(RowFacts(table="rejected_messages", direction="outbound")) == "rejected"


@pytest.mark.parametrize("status", ["sending", "sent", "failed"])
def test_outbound_message_row_status_is_state(status: str) -> None:
    # design.md §2.1 / §2.3：sending / sent / failed 就是 messages.status 的
    # 列值，库内与对外同一套取值、没有映射层（failed 含退信到达后自 sent
    # 迁入的行——判据是列值，与 sent_at 无关）。
    assert (
        determine_state(RowFacts(table="messages", direction="outbound", status=status)) == status
    )


def test_inbound_message_row_is_received() -> None:
    # design.md §2.1：received = 入站回复行，写入即终态，不属于出站状态机。
    assert (
        determine_state(RowFacts(table="messages", direction="inbound", status="received"))
        == "received"
    )


def test_negative_attempt_count_rejected() -> None:
    # attempt_count 建行即 0、只会递增（design.md §2.4）；负数是调用方构造
    # 错误，直接失败、不猜。
    with pytest.raises(ValueError, match="attempt_count"):
        determine_state(RowFacts(table="pending_messages", direction="outbound", attempt_count=-1))


def test_outbound_row_with_received_status_rejected() -> None:
    # design.md §2.2 行模型 CHECK：出站行不得为 received。
    with pytest.raises(ValueError, match="出站行"):
        determine_state(RowFacts(table="messages", direction="outbound", status="received"))


def test_inbound_row_with_non_received_status_rejected() -> None:
    # design.md §2.2 行模型 CHECK：入站行只能 received。
    with pytest.raises(ValueError, match="入站行"):
        determine_state(RowFacts(table="messages", direction="inbound", status="sending"))


def test_outbound_row_with_unknown_status_rejected() -> None:
    # 列值受 CHECK 约束（constants.MESSAGES_STATUS_VALUES）；未知值进判定
    # 即调用方构造错误。
    with pytest.raises(ValueError, match="出站行"):
        determine_state(RowFacts(table="messages", direction="outbound", status="queued"))


def test_non_mail_table_rejected() -> None:
    # design.md §1：imap_cursors 不承载邮件行；只有三张邮件表参与六态判定。
    with pytest.raises(ValueError, match="不承载邮件行的表"):
        determine_state(RowFacts(table="imap_cursors", direction="outbound"))


def test_state_locations_cover_full_vocabulary() -> None:
    # 物理落点映射必须恰好覆盖判定词表全集（六态 + 入站终态）：缺一行，
    # 排查时该状态就找不到物理落点。
    assert set(STATE_LOCATIONS) == set(constants.MESSAGE_STATES) | {constants.RECEIVED_STATE}
