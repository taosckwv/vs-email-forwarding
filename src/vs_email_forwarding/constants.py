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

#: 六态的具名常量。**具名常量是取值的唯一入口**：消费侧一律取名字，不用位置下标——下标在
#: 词表中间插入一项时会一起指错，而 `ruff` / `mypy` 都查不出来（守护用例见
#: `tests/unit/test_constants_hygiene.py`）。
MESSAGE_STATE_SUBMITTED: MessageState = "submitted"
MESSAGE_STATE_REVIEWING: MessageState = "reviewing"
MESSAGE_STATE_REJECTED: MessageState = "rejected"
MESSAGE_STATE_SENDING: MessageState = "sending"
MESSAGE_STATE_SENT: MessageState = "sent"
MESSAGE_STATE_FAILED: MessageState = "failed"

#: 出站邮件的六态取值集合（顺序即对外文档里的推荐展示顺序）。
MESSAGE_STATES: tuple[MessageState, ...] = (
    MESSAGE_STATE_SUBMITTED,
    MESSAGE_STATE_REVIEWING,
    MESSAGE_STATE_REJECTED,
    MESSAGE_STATE_SENDING,
    MESSAGE_STATE_SENT,
    MESSAGE_STATE_FAILED,
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

#: 投递三态（`sending` / `sent` / `failed`）：`messages.status` 的列值集合由它拼出，
#: 顺序即状态机推进顺序。它的存在让「取六态里的后三态」不再写成数字切片。
MESSAGE_DELIVERY_STATES: tuple[MessageState, ...] = (
    MESSAGE_STATE_SENDING,
    MESSAGE_STATE_SENT,
    MESSAGE_STATE_FAILED,
)

#: `messages.status` 列的 `CHECK` 取值集合：出站三态与入站终态同列共存。
MESSAGES_STATUS_VALUES: tuple[str, ...] = (*MESSAGE_DELIVERY_STATES, RECEIVED_STATE)

#: 行方向取值。**具名常量是取值的唯一入口**：消费侧一律取名字，不用位置下标——下标在词表
#: 中间插入一项时会一起指错，而 `ruff` / `mypy` 都查不出来（守护用例见 `tests/unit/
#: test_constants_hygiene.py`）。
Direction = Literal["outbound", "inbound"]
DIRECTION_OUTBOUND: Direction = "outbound"
DIRECTION_INBOUND: Direction = "inbound"
DIRECTIONS: tuple[Direction, ...] = (DIRECTION_OUTBOUND, DIRECTION_INBOUND)

#: 正文格式取值（提交请求与回复响应共用）。
BodyFormat = Literal["text", "html"]
BODY_FORMAT_TEXT: BodyFormat = "text"
BODY_FORMAT_HTML: BodyFormat = "html"
BODY_FORMATS: tuple[BodyFormat, ...] = (BODY_FORMAT_TEXT, BODY_FORMAT_HTML)
DEFAULT_BODY_FORMAT: BodyFormat = BODY_FORMAT_TEXT

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

DISCARD_REASON_NO_REFERENCE: DiscardReason = "no_reference"
DISCARD_REASON_NO_MATCH: DiscardReason = "no_match"
DISCARD_REASON_MDN: DiscardReason = "mdn"
DISCARD_REASON_DSN_WITHOUT_ID: DiscardReason = "dsn_without_id"
DISCARD_REASON_DSN_TARGET_GONE: DiscardReason = "dsn_target_gone"
DISCARD_REASON_DSN_TARGET_NOT_SENT: DiscardReason = "dsn_target_not_sent"
DISCARD_REASON_DUPLICATE: DiscardReason = "duplicate"
DISCARD_REASON_NO_REPLY_FROM: DiscardReason = "no_reply_from"
DISCARD_REASON_MALFORMED_REFERENCE: DiscardReason = "malformed_reference"
DISCARD_REASON_REPLY_TARGET_GONE: DiscardReason = "reply_target_gone"
DISCARD_REASON_UNPARSABLE: DiscardReason = "unparsable"

#: 十一类丢弃原因，顺序与设计文档的枚举顺序一致。
DISCARD_REASONS: tuple[DiscardReason, ...] = (
    DISCARD_REASON_NO_REFERENCE,
    DISCARD_REASON_NO_MATCH,
    DISCARD_REASON_MDN,
    DISCARD_REASON_DSN_WITHOUT_ID,
    DISCARD_REASON_DSN_TARGET_GONE,
    DISCARD_REASON_DSN_TARGET_NOT_SENT,
    DISCARD_REASON_DUPLICATE,
    DISCARD_REASON_NO_REPLY_FROM,
    DISCARD_REASON_MALFORMED_REFERENCE,
    DISCARD_REASON_REPLY_TARGET_GONE,
    DISCARD_REASON_UNPARSABLE,
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
ERROR_CLASS_PERMANENT: ErrorClass = "permanent"
ERROR_CLASS_TRANSIENT: ErrorClass = "transient"
ERROR_CLASS_INTERNAL: ErrorClass = "internal"

#: 失败分类取值：`permanent` / `transient` / `internal`（审查环节异常的独立分类）。
ERROR_CLASSES: tuple[ErrorClass, ...] = (
    ERROR_CLASS_PERMANENT,
    ERROR_CLASS_TRANSIENT,
    ERROR_CLASS_INTERNAL,
)

#: SMTP 返回码区间：`4xx` 可重试、`5xx` 不可重试（`FR-5` / `FR-6` 共用同一分类）。
SMTP_TRANSIENT_MIN: int = 400
SMTP_TRANSIENT_MAX: int = 499
SMTP_PERMANENT_MIN: int = 500
SMTP_PERMANENT_MAX: int = 599

# ---------------------------------------------------------------------------
# 拒审来源
# ---------------------------------------------------------------------------

RejectSource = Literal["review_rejected", "review_error", "review_incomplete"]
REJECT_SOURCE_REVIEW_REJECTED: RejectSource = "review_rejected"
REJECT_SOURCE_REVIEW_ERROR: RejectSource = "review_error"
REJECT_SOURCE_REVIEW_INCOMPLETE: RejectSource = "review_incomplete"

#: `rejected_messages.source` 的取值：判定拒 / 审查出错用尽重试 / 收割支推进终态。
REJECT_SOURCES: tuple[RejectSource, ...] = (
    REJECT_SOURCE_REVIEW_REJECTED,
    REJECT_SOURCE_REVIEW_ERROR,
    REJECT_SOURCE_REVIEW_INCOMPLETE,
)

# ---------------------------------------------------------------------------
# 保留期（`NFR-8`）
# ---------------------------------------------------------------------------

#: 邮件行与拒审行的保留期（天）：`NFR-8` 约定「保留 30 天」。它是**产品语义**，不是
#: 「随环境合法地不同」的值，因此落本模块而不进 `.env`（`AGENTS.md` §7 的判定口径）。
#: 可配置的是清理**间隔**（`POLL_CLEANUP_SECONDS`），保留期本身不设配置项——否则
#: 「保留多少天」会变成部署差异，同一份对外承诺在不同环境给出不同答案。
RETENTION_DAYS: int = 30

#: 时间戳的对外格式：UTC 的 RFC 3339、固定六位小数、以 `Z` 结尾（`NFR-7` / `AC-17`）。
TIMESTAMP_FORMAT: str = "%Y-%m-%dT%H:%M:%S.%fZ"

__all__ = [
    "BODY_FORMATS",
    "BODY_FORMAT_HTML",
    "BODY_FORMAT_TEXT",
    "DEFAULT_BODY_FORMAT",
    "DIRECTIONS",
    "DIRECTION_INBOUND",
    "DIRECTION_OUTBOUND",
    "DISCARD_REASONS",
    "DISCARD_REASON_DSN_TARGET_GONE",
    "DISCARD_REASON_DSN_TARGET_NOT_SENT",
    "DISCARD_REASON_DSN_WITHOUT_ID",
    "DISCARD_REASON_DUPLICATE",
    "DISCARD_REASON_MALFORMED_REFERENCE",
    "DISCARD_REASON_MDN",
    "DISCARD_REASON_NO_MATCH",
    "DISCARD_REASON_NO_REFERENCE",
    "DISCARD_REASON_NO_REPLY_FROM",
    "DISCARD_REASON_REPLY_TARGET_GONE",
    "DISCARD_REASON_UNPARSABLE",
    "ERROR_CLASSES",
    "ERROR_CLASS_INTERNAL",
    "ERROR_CLASS_PERMANENT",
    "ERROR_CLASS_TRANSIENT",
    "MAX_ATTEMPTS",
    "MAX_BACKOFF_ATTEMPT",
    "MAX_CLAIMABLE_ATTEMPT_COUNT",
    "MESSAGES_STATUS_VALUES",
    "MESSAGE_DELIVERY_STATES",
    "MESSAGE_STATES",
    "MESSAGE_STATE_FAILED",
    "MESSAGE_STATE_LABELS",
    "MESSAGE_STATE_REJECTED",
    "MESSAGE_STATE_REVIEWING",
    "MESSAGE_STATE_SENDING",
    "MESSAGE_STATE_SENT",
    "MESSAGE_STATE_SUBMITTED",
    "MESSAGE_STATE_TRANSITIONS",
    "RECEIVED_STATE",
    "REJECT_SOURCES",
    "REJECT_SOURCE_REVIEW_ERROR",
    "REJECT_SOURCE_REVIEW_INCOMPLETE",
    "REJECT_SOURCE_REVIEW_REJECTED",
    "RETENTION_DAYS",
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
