#!/usr/bin/env python3
"""审查供应商的接口与中性输入对象（`design.md` §4.2）。

一期的审查实现是显式标注的**空实现**（`always_pass.py`，**永远通过**）；真实
审查能力（本地规则还是外部服务）尚未定案，见 `docs/open-questions.md`。接口
按 §4.2 先定型：中性对象进、受控枚举出——既不接收 ORM 行，也不接收数据库
会话，`providers/` 不得依赖 `db/`，否则适配器就与存储实现绑死了。

输出不设原因文本、不设风险分类字段：原因文本若要入库，与 `design.md` §2.4
「错误里不落任何自由文本」的取向冲突；分类字段目前没有任何消费方。返回
`reject` 与抛异常（含超时）是两条不同路径，由接口本身区分，因此也不需要
「错误」字段。超时上界 `REVIEW_TIMEOUT_SECONDS` 属配置层，由调用方施加，
接口不读配置。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from vs_email_forwarding.constants import BodyFormat, Direction

__all__ = ["ReviewDecision", "ReviewMessage", "Reviewer"]

# 审查判定的受控枚举（design.md §4.2：pass 放行 / reject 拒审）。constants.py
# 未收录该词表（§4.2 把它定在接口本身），故在接口处固定——与 core 各模块本地
# Literal 的先例一致，运行期无第二份取值来源。
type ReviewDecision = Literal["pass", "reject"]


@dataclass(frozen=True, kw_only=True)
class ReviewMessage:
    """一封待审邮件的中性输入（`design.md` §4.2 的固定六字段，一期空实现忽略全部字段）。

    审查要审的正是它即将决定放行的那份内容，所以内容必须完整传入；
    `message_id` 是 `NFR-5` 要求的「用 ID 引用邮件」——适配器记录自己的判定
    或告警时靠它，且它不携带任何内容信息。
    """

    #: 出站 / 入站；真实审查器据此对两者采用不同策略。
    direction: Direction

    #: 该邮件的对外 ID（即主键），不携带任何内容信息。
    message_id: UUID

    #: 即将被放行的那份内容四字段。
    recipient: str
    subject: str
    body: str
    body_format: BodyFormat


class Reviewer(Protocol):
    """审查器接口：只回答「过 / 不过」，不编排状态流转（那属 `flows/`）。"""

    async def review(self, message: ReviewMessage) -> ReviewDecision:
        """审一封待放行的邮件。

        返回 ``pass`` → 放行；返回 ``reject`` → 判定为拒（写
        `rejected_messages`，`source` 记 `review_rejected`）。抛异常 → 审查
        环节出错（含超时），按固定重试语义重试，用尽后 `source` 记
        `review_error`。两条路径的处置都由 `flows/` 的审查流编排。
        """
        ...
