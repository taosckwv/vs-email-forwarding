#!/usr/bin/env python3
"""重试退避算术与 SMTP 返回码分类（纯逻辑）。

本模块只写算术：退避秒数、尝试上限与 ``4xx`` / ``5xx`` 区间一律取自
:mod:`vs_email_forwarding.constants`（取值真源是
``docs/research/02-runtime-and-scheduling.md``，由 ``core/retry.py`` 集中提供、
不得在业务流程里硬编码，`plan.md` §5.2）。时间由调用方作为参数传入——认领是
短事务，取时点只有认领事务那一处（同节），纯逻辑不自行取时、不碰时钟。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from vs_email_forwarding.constants import (
    MAX_ATTEMPTS,
    RETRY_BACKOFF_SECONDS,
    SMTP_PERMANENT_MAX,
    SMTP_PERMANENT_MIN,
    SMTP_TRANSIENT_MAX,
    SMTP_TRANSIENT_MIN,
)

__all__ = ["SmtpErrorClass", "classify_smtp_code", "next_attempt_at"]

#: SMTP 失败返回码的两类受控分类（`design.md` §2.4）。``internal`` 留给服务
#: 自身故障（审查环节异常、代码错误），不由 SMTP 返回码产生，故不在此列。
SmtpErrorClass = Literal["transient", "permanent"]


def next_attempt_at(now: datetime, attempt: int) -> datetime:
    """认领第 ``attempt`` 次后应写入的 ``next_attempt_at``。

    规则（`design.md` §3 阶段① / `plan.md` §5.2）：认领第 n 次即
    ``next_attempt_at = now + backoff[n]``（n = 1 / 2 / 3）；第 4 次（上限）
    **不推**——该行只剩终态一条路，收割支本就不写这一列。``now`` 是认领事务
    取得的时刻（aware UTC），由调用方传入；裸 ``datetime`` 直接拒绝
    （`AGENTS.md` §7「时间一律带时区」）。
    """
    if now.tzinfo is None:
        raise ValueError("now 必须是带时区的 datetime")
    if not 1 <= attempt <= MAX_ATTEMPTS:
        raise ValueError(f"尝试次数必须在 1..{MAX_ATTEMPTS} 之间：{attempt}")
    backoff_seconds = RETRY_BACKOFF_SECONDS.get(attempt)
    if backoff_seconds is None:
        return now  # 第 4 次不推退避（退避表只覆盖 1..3）。
    return now + timedelta(seconds=backoff_seconds)


def classify_smtp_code(code: int) -> SmtpErrorClass:
    """把 SMTP 失败返回码分类为 ``transient`` / ``permanent``。

    区间取自 constants：``4xx`` 是对端临时故障（可重试）、``5xx`` 是对端永久
    拒绝（不可重试，`design.md` §2.4）。``5xx`` 直接置终态、不再消耗剩余重试
    这条例外见 `design.md` §3.3 的处置表与 `plan.md` §5.2。
    两个区间之外（``2xx`` / ``3xx`` 等成功类）不是失败码——失败分类器收到它
    即调用方契约违规，直接抛 :class:`ValueError`，不做「默认当作可重试」的
    兜底。
    """
    if SMTP_TRANSIENT_MIN <= code <= SMTP_TRANSIENT_MAX:
        return "transient"
    if SMTP_PERMANENT_MIN <= code <= SMTP_PERMANENT_MAX:
        return "permanent"
    raise ValueError(f"SMTP 返回码 {code} 不在失败码区间（4xx / 5xx）")
