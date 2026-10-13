#!/usr/bin/env python3
"""发信适配器的接口与中性对象（`design.md` §3.3、§4.3）。

发信走 SMTP 直连（企业邮箱 / 自建 MTA），编排放 `flows/`、重试算术放
`core/retry.py`；适配器只做一件事：**把一封中性邮件投出去，并回报受控的
判定结果**。配置一律由调用方读好、经构造函数注入（`design.md` §4.5：配置
唯一入口是 `config.py`），本模块不读环境变量、不 import `config`。

两条硬约束（`AGENTS.md` §8 第 3、4 条）：

* **发件人身份只能来自服务配置**——`From` 取注入的 `MAIL_FROM_ADDRESS`，
  **显式**写入 `Reply-To` = 注入的 `REPLY_TO_ADDRESS`（`design.md` §4.3：
  不显式声明时，回路会隐式依赖「`From` 恰好等于被拉取的信箱」，此后任何
  一次改动 `From` 都会静默断掉回复）。两者都不在 `OutboundMessage` 里，
  调用方**没有**可以覆盖它们的入口。
* **出错只回传 SMTP 返回码与受控分类**——不落对端的原始回复文本，也不落
  任何自由文本（`design.md` §2.4 的「错误里不落任何自由文本」）。对端的
  原始回复可能回显地址乃至正文，落库即违规（`AC-30`）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from vs_email_forwarding.constants import BodyFormat, ErrorClass

__all__ = ["OutboundMessage", "SmtpDeliveryResult", "SmtpSender", "accepted", "rejected"]


@dataclass(frozen=True, kw_only=True)
class OutboundMessage:
    """一封待发邮件的中性内容（`design.md` §4.3）。

    只有收件人与内容四字段。`From` 与 `Reply-To` **不在这里**：它们由适配器
    从注入的服务配置显式写入，调用方无从指定（`AGENTS.md` §8 第 3 条）。
    """

    #: 收件人地址；一期只支持单收件人，没有抄送密送（`design.md` §4.3）。
    recipient: str

    #: 主题与正文；`body_format` 决定正文按 `text/plain` 还是 `text/html` 发出。
    subject: str
    body: str
    body_format: BodyFormat


@dataclass(frozen=True, kw_only=True)
class SmtpDeliveryResult:
    """一次投递尝试的结果：**只**含 SMTP 返回码与受控分类，没有别的字段。

    三种情形（`design.md` §3.3 的处置表）：

    * 投递被接受 → ``accepted=True``、无错误分类；
    * 对端拒绝（``4xx`` / ``5xx``）→ ``accepted=False``、``code`` 为该返回码；
    * 连接 / 超时类故障（SMTP 交互根本没完成）→ ``accepted=False``、
      ``code is None``。

    ``error_class`` 就是 `messages.last_error_class` 的列值、``code`` 就是
    `messages.last_smtp_code` 的列值（`design.md` §2.4）——分类口径与返回码
    区间由 `constants.py` 的 ``SMTP_TRANSIENT_*`` / ``SMTP_PERMANENT_*`` 给出，
    本包按同一组常量分类（`docs/architecture.md` §2：`providers/` 不得依赖
    `core/`）；重试侧的算术在 `core/retry.py`，与本包各从同一组常量取值，
    业务流程里不再现分一次。

    字段不设 setter，实例只由 :func:`accepted` / :func:`rejected` 构造，因此
    「``accepted=False`` 却没有错误分类」这种自相矛盾的记录造不出来。
    """

    #: 对端是否接受了这封信（`design.md` §3.3「成功 → status = sent」）。
    accepted: bool

    #: SMTP 协议返回码（如 ``451`` / ``550``）；连接 / 超时类故障为 ``None``。
    code: int | None = None

    #: 失败分类（`constants.ErrorClass`）；成功时为 ``None``。
    error_class: ErrorClass | None = None


def accepted() -> SmtpDeliveryResult:
    """投递被对端接受（无返回码、无错误分类）。"""
    return SmtpDeliveryResult(accepted=True)


def rejected(*, code: int | None, error_class: ErrorClass) -> SmtpDeliveryResult:
    """投递被拒或故障中止；``code=None`` 表示 SMTP 交互未完成（连接 / 超时）。"""
    return SmtpDeliveryResult(accepted=False, code=code, error_class=error_class)


class SmtpSender(Protocol):
    """发信适配器接口：投一封、回报一次判定（`design.md` §3.3）。"""

    async def send_message(
        self, message: OutboundMessage, *, timeout_seconds: int | None = None
    ) -> SmtpDeliveryResult:
        """投递一封邮件，返回受控判定结果。

        实现必须把它变成 I/O 的失败（连接、超时、对端拒绝）**全部**收进返回
        值，不把异常抛给调用方——本接口的契约是「一次调用给出一个受控判定」。
        ``timeout_seconds`` 是单次调用的墙钟上界（`SMTP_TIMEOUT_SECONDS`，
        `design.md` §3 阶段②）；传 ``None`` 时用实现自己的默认上界。

        重试语义、``5xx`` 直接终态、收割支与状态落库都由 `flows/` 的编排
        决定（`design.md` §3.3），不在适配器里——适配器只回答「这次投递结果
        是什么」。
        """
        ...
