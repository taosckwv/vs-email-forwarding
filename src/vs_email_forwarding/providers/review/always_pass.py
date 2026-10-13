#!/usr/bin/env python3
"""审查的**空实现**：**永远通过**（`design.md` §4.2，`NFR-6` / `AC-25`）。

一期没有真实审查能力（本地规则还是外部审查服务尚未定案，见
`docs/open-questions.md`）：本实现不读任何输入字段、恒返回 `pass`。它**不是**
内容安全边界——在真实的敏感信息 / 病毒审查接入之前，不要把本服务当作内容
安全边界使用（`docs/security.md` 的安全边界一节）。实现由
`providers/review/factory.py` 的工厂选取，一期恒为本空实现。
"""

from __future__ import annotations

from vs_email_forwarding.providers.review.base import ReviewDecision, ReviewMessage

__all__ = ["AlwaysPassReviewer"]


class AlwaysPassReviewer:
    """恒返回 ``pass`` 的空实现（一期工厂的默认实现）。"""

    async def review(self, message: ReviewMessage) -> ReviewDecision:
        """空实现：不读输入、恒放行。"""
        return "pass"
