#!/usr/bin/env python3
"""服务级常量：**共享叶子层**。

本模块只放**服务的固定语义**——取值的文本、重试算术、受控枚举集合。它是全项目最低层：
**不 import 任何项目内模块、不做任何 I/O**，因此各层（含 `db/`）都可以 import 它。

## 什么该放这里，什么不该

* **放这里**：改了它等于改**产品行为**的值。例如状态取值、退避秒数、重试上限、丢弃原因标识符。
  这些不进 `.env`：`AGENTS.md` §8 把重试语义定为硬约束，放进环境变量等于让「跑在哪个环境」
  改变服务语义，且会破坏「配置写错即启动失败」的取向。
* **不放这里**：**随环境合法地不同**的值（监听地址、SMTP / IMAP 主机与账号、连接串、发件人地址、
  轮询间隔、单次调用超时、`BATCH_SIZE`、内容长度上限、回复分页条数）。它们在 `config.py`
  从 `.env` 读，唯一入口是 :func:`vs_email_forwarding.config.get_settings`。

## 只放值，不放规则

判定逻辑（六态判定的优先级、`Message-ID` 的解析、退信结构判定、入站分拣）属 `core/` 的
其他模块——本模块里的枚举是它们的输入，不是它们的替身。

## 单一真源与重复文本

对外字段与取值约束的真源是 `docs/api-contract.yaml`。本模块与 DDL 的 `CHECK` 各自持有
同一串取值的文本（DDL 必须自洽、不能依赖 Python 常量），由
`tests/unit/test_constants_consistency.py` 断言「契约 `enum` == 本模块取值集合 == 迁移里的
`CHECK` 集合」三方一致；**不要**为了消掉文本重复而让 DDL 去 import 本模块。
"""

from __future__ import annotations

from typing import Literal

# ---------------------------------------------------------------------------
# 状态取值（库内与对外同一套，不存在映射层）
#
# 真源说明见 specs/0001-email-forwarding-service/design.md §2.1 落点表与 §2.3 状态机：
#   * submitted / reviewing 由「行在 pending_messages + attempt_count」表达；
#   * rejected 由「行在 rejected_messages」表达；
#   * sending / sent / failed 是 messages.status 的列值；
#   * received 是入站回复行的终态，**不属于出站状态机**。
# ---------------------------------------------------------------------------

MessageState = Literal["submitted", "reviewing", "rejected", "sending", "sent", "failed"]

#: 出站邮件的六态取值集合（顺序即对外文档里的推荐展示顺序）。
MESSAGE_STATES: tuple[MessageState, ...] = (
    "submitted",
    "reviewing",
    "rejected",
    "sending",
    "sent",
    "failed",
)

#: 六态的中文名。对外文档与日志用它，避免各处自行翻译出不同说法。
MESSAGE_STATE_LABELS: dict[MessageState, str] = {
    "submitted": "已提交",
    "reviewing": "审查中",
    "rejected": "拒审",
    "sending": "投递中",
    "sent": "已发出",
    "failed": "发送失败",
}

#: 出站行的合法迁移起点与终点（`AGENTS.md` §2 的带谓词条件 UPDATE 只允许这些组合）。
#: 入站行为 `received`，写入即终态、不迁移。
MESSAGE_STATE_TRANSITIONS: tuple[tuple[MessageState, MessageState], ...] = (
    ("sending", "sent"),
    ("sending", "failed"),
    ("sent", "failed"),
)

#: 入站回复行的状态取值（与出站六态同属一个词汇表，但不参与出站状态机）。
RECEIVED_STATE: Literal["received"] = "received"

#: `messages.status` 列的 `CHECK` 取值集合：出站三态与入站终态同列共存。
MESSAGES_STATUS_VALUES: tuple[str, ...] = (*MESSAGE_STATES[3:], RECEIVED_STATE)

#: 行方向取值。
Direction = Literal["outbound", "inbound"]
DIRECTIONS: tuple[Direction, ...] = ("outbound", "inbound")

#: 正文格式取值（提交请求与回复响应共用）。
BodyFormat = Literal["text", "html"]
BODY_FORMATS: tuple[BodyFormat, ...] = ("text", "html")
DEFAULT_BODY_FORMAT: BodyFormat = "text"

# ---------------------------------------------------------------------------
# 丢入站邮件时的受控原因（十一类）
#
# 口径见 specs/0001-email-forwarding-service/design.md §3.4：丢弃一律不入库，
# 只在日志里按原因分类计数（`AC-13` / `AC-28`），且不得记录任何可回溯字段。
# ---------------------------------------------------------------------------

DiscardReason = Literal[
    "no_reference",
    "no_match",
    "mdn",
    "dsn_without_id",
    "dsn_target_gone",
    "dsn_target_not_sent",
    "duplicate",
    "no_reply_from",
    "malformed_reference",
    "reply_target_gone",
    "unparsable",
]

#: 十一类丢弃原因，顺序与设计文档的枚举顺序一致。
DISCARD_REASONS: tuple[DiscardReason, ...] = (
    "no_reference",
    "no_match",
    "mdn",
    "dsn_without_id",
    "dsn_target_gone",
    "dsn_target_not_sent",
    "duplicate",
    "no_reply_from",
    "malformed_reference",
    "reply_target_gone",
    "unparsable",
)

# ---------------------------------------------------------------------------
# 重试语义（硬约束，不得改成配置项）
#
# 真源：AGENTS.md §8、docs/research/02-runtime-and-scheduling.md。
# 首次尝试 + 3 次重试 = 最多 4 次尝试；退避 15s / 30s / 60s。
# ---------------------------------------------------------------------------

#: 允许的最大尝试次数（含首次）。
MAX_ATTEMPTS: int = 4

#: 认领谓词的上界：`attempt_count <= MAX_ATTEMPTS`。
#: 放宽到 `<= 4` 是为了让「第 4 次认领后崩溃」的遗留行仍能被认领、进而由收割支推进终态。
MAX_CLAIMABLE_ATTEMPT_COUNT: int = MAX_ATTEMPTS

#: 第 n 次尝试后到下一次尝试之前的退避秒数。
#: 下标 n 对应 `attempt_count = n`；第 4 次不再退避（该行只剩终态一条路）。
RETRY_BACKOFF_SECONDS: dict[int, int] = {1: 15, 2: 30, 3: 60}

#: 第五个参数：退避表的最大下标（`MAX_ATTEMPTS - 1`），供实现自查使用。
MAX_BACKOFF_ATTEMPT: int = MAX_ATTEMPTS - 1

# ---------------------------------------------------------------------------
# SMTP 失败的受控分类
#
# 只允许落这两类与 SMTP 返回码（`AGENTS.md` §8、`FR-5`）：
# 绝不记录对方服务器的原始回复文本（可能回显地址乃至正文）。
# ---------------------------------------------------------------------------

ErrorClass = Literal["permanent", "transient", "internal"]

#: 失败分类取值：`permanent` / `transient` / `internal`（审查环节异常的独立分类）。
ERROR_CLASSES: tuple[ErrorClass, ...] = ("permanent", "transient", "internal")

#: SMTP 返回码区间：`4xx` 可重试、`5xx` 不可重试（`FR-5` / `FR-6` 共用同一分类）。
SMTP_TRANSIENT_MIN: int = 400
SMTP_TRANSIENT_MAX: int = 499
SMTP_PERMANENT_MIN: int = 500
SMTP_PERMANENT_MAX: int = 599

# ---------------------------------------------------------------------------
# 拒审来源
# ---------------------------------------------------------------------------

RejectSource = Literal["review_rejected", "review_error", "review_incomplete"]

#: `rejected_messages.source` 的取值：判定拒 / 审查出错用尽重试 / 收割支推进终态。
REJECT_SOURCES: tuple[RejectSource, ...] = (
    "review_rejected",
    "review_error",
    "review_incomplete",
)

#: 时间戳的对外格式：UTC 的 RFC 3339、固定六位小数、以 `Z` 结尾（`NFR-7` / `AC-17`）。
TIMESTAMP_FORMAT: str = "%Y-%m-%dT%H:%M:%S.%fZ"

__all__ = [
    "BODY_FORMATS",
    "DEFAULT_BODY_FORMAT",
    "DIRECTIONS",
    "DISCARD_REASONS",
    "ERROR_CLASSES",
    "MAX_ATTEMPTS",
    "MAX_BACKOFF_ATTEMPT",
    "MAX_CLAIMABLE_ATTEMPT_COUNT",
    "MESSAGES_STATUS_VALUES",
    "MESSAGE_STATES",
    "MESSAGE_STATE_LABELS",
    "MESSAGE_STATE_TRANSITIONS",
    "RECEIVED_STATE",
    "REJECT_SOURCES",
    "RETRY_BACKOFF_SECONDS",
    "SMTP_PERMANENT_MAX",
    "SMTP_PERMANENT_MIN",
    "SMTP_TRANSIENT_MAX",
    "SMTP_TRANSIENT_MIN",
    "TIMESTAMP_FORMAT",
    "BodyFormat",
    "Direction",
    "DiscardReason",
    "ErrorClass",
    "MessageState",
    "RejectSource",
]
