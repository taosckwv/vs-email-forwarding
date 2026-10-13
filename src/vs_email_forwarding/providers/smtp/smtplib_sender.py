#!/usr/bin/env python3
"""基于标准库 `smtplib` 的发信实现（`design.md` §3.3、§4.3，`plan.md` §2.3）。

`smtplib` 是同步库，阻塞调用不得放进事件循环，因此整个投递经
:func:`asyncio.to_thread` 执行（与 IMAP 侧同构，`design.md` §3.3），并且两层
上界都由注入的 `SMTP_TIMEOUT_SECONDS` 给出（`design.md` §3 阶段②）：
`smtplib.SMTP` 的套接字超时，以及 :func:`asyncio.wait_for` 的墙钟上界。两者
都由调用方传入——本模块不读配置、不读环境变量：`config.py` 是唯一配置入口
（`design.md` §4.5），装配在阶段 4 的 `flows/`。

**身份只来自注入值**（`AGENTS.md` §8 第 3 条，`design.md` §4.3）：报文头的
`From`、SMTP 信封发件人（`MAIL FROM`）都取 `SmtpConfig.mail_from`；`Reply-To`
**总是显式写入**、取 `SmtpConfig.reply_to`。调用方的 `OutboundMessage` 里
没有这两个字段，也没有别的途径能改动它们——「调用方指定发件人或回复地址」
在接口形状上就不成立。

**失败只回传返回码与分类**：对端有返回码的拒绝有四种形态（收件人被拒
`smtplib.SMTPRecipientsRefused`、发件人被拒 `SMTPSenderRefused`、其余
`SMTPResponseException`，以及 greeting 期的 `SMTPConnectError`），都取出
``smtp_code``，由本模块的 :func:`_classify_smtp_code` 按 `constants.py` 的
区间常量分类（`design.md` §3.3 处置表：``5xx`` → permanent、``4xx`` 与
连接 / 超时类故障 → transient）。**边界是全量的**：``400..599`` 之外的返回码
在处置表里没有对应行，按可重试的 transient 记、并保留实测返回码
（`design.md` §2.4 的 `last_smtp_code` 列语义）——`providers/smtp/base.py` 的
接口契约要求把一次调用的结果收进返回值，不把异常抛给调用方。

`core/retry.py` 的同名分类器服务 flows 侧的失败码入口，越界即判为调用方契约
违规、直接抛 `ValueError`——两者刻意不同；并且 `docs/architecture.md` §2 把
`core/` 列在 `providers/` 的「不得依赖」列，本模块不 import 它。

连接与超时类故障（`OSError` / `TimeoutError`，`smtplib` 的异常本身也是
`OSError` 的子类）一律归 transient、``code`` 为 ``None``——SMTP 交互没完成，
没有返回码可记。**对端的原始回复文本在每一处都被丢弃**：不进返回值、不进
日志、不进异常（`design.md` §2.4；`AC-30` 断言全表不出现它）。
"""

from __future__ import annotations

import asyncio
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Literal

from vs_email_forwarding.constants import (
    SMTP_PERMANENT_MAX,
    SMTP_PERMANENT_MIN,
    SMTP_TRANSIENT_MAX,
    SMTP_TRANSIENT_MIN,
    BodyFormat,
)
from vs_email_forwarding.providers.smtp.base import (
    OutboundMessage,
    SmtpDeliveryResult,
    accepted,
    rejected,
)

__all__ = ["SmtpConfig", "SmtplibSender"]

#: 正文格式到 MIME 子类型的映射（`constants.BodyFormat` 的两个取值，一期形态
#: 只有纯文本与 HTML，`design.md` §4.3）。新增正文格式时这里会因 mypy 而红。
_MIME_SUBTYPES: dict[BodyFormat, str] = {"text": "plain", "html": "html"}

#: SMTP 失败返回码的两类受控分类（`design.md` §2.4）。类型在本地声明、不取
#: `core/retry.py` 的 `SmtpErrorClass`：`docs/architecture.md` §2 的依赖表把
#: `core/` 列在 `providers/` 的「不得依赖」列，接口层跨包取类型即违例；先例
#: 是 `core/inbound.py` 的 `DsnStatusClass` 与 `providers/review/base.py` 的
#: `ReviewDecision`（词表窄到本层实际产出的取值，运行期无第二份取值来源）。
#: 取值真源仍是 `constants.py` 的区间常量——两个词表由同一组常量派生。
SmtpErrorClass = Literal["transient", "permanent"]


def _classify_smtp_code(code: int) -> SmtpErrorClass:
    """按 `constants.py` 的区间常量把 SMTP 失败码分类（`design.md` §2.4）。

    `design.md` §3.3 的处置表只有两行：``5xx`` 记 permanent（`AC-34`：直接置
    终态，不再消耗剩余重试次数），``4xx`` 与连接 / 超时类故障记 transient。
    `AGENTS.md` §8 第 7 条的重试语义正是按这两行分岔，``5xx`` 是其中的例外。

    区间之外的返回码在处置表里没有对应行：**适配器的边界是全量的**，按可重试
    的 transient 记并保留实测返回码（`design.md` §2.4 的 `last_smtp_code` 列
    语义），绝不把异常抛给调用方（`providers/smtp/base.py` 的接口契约）。这一点
    与 `core/retry.classify_smtp_code` 刻意不同——后者服务 flows 侧的失败码入
    口，越界即调用方契约违规、直接抛 `ValueError`。
    """
    if SMTP_TRANSIENT_MIN <= code <= SMTP_TRANSIENT_MAX:
        return "transient"
    if SMTP_PERMANENT_MIN <= code <= SMTP_PERMANENT_MAX:
        return "permanent"
    # 区间之外：没有可依据的处置行，按可重试计，但把实测返回码留给落库。
    return "transient"


@dataclass(frozen=True, kw_only=True)
class SmtpConfig:
    """`smtplib` 实现所需的全部注入参数（`design.md` §4.5 的参数来源）。

    由调用方（阶段 4 的 `flows/`）从 `config.get_settings()` 读好再传入：
    本模块与 `providers/` 其它模块一样不碰配置层，也不读环境变量。
    """

    #: SMTP 主机与端口（`SMTP_HOST` / `SMTP_PORT`）。
    host: str
    port: int

    #: 登录名与口令（`SMTP_USERNAME` / `SMTP_PASSWORD`）。口令只用于登录，
    #: 绝不出现在返回结果、日志或异常里（`NFR-5`）；登录名为空则跳过登录。
    username: str
    password: str

    #: 是否用 STARTTLS 升级连接（`SMTP_STARTTLS`）。
    starttls: bool

    #: 单次投递的墙钟上界（秒，`SMTP_TIMEOUT_SECONDS`；`send_message` 的实参
    #: 未给时用它）。
    timeout_seconds: int

    #: 发件人身份：`From` / `MAIL FROM` 与显式写入的 `Reply-To`，分别取自
    #: `MAIL_FROM_ADDRESS` 与 `REPLY_TO_ADDRESS`，**不允许调用方指定**。
    mail_from: str
    reply_to: str


class _ResponseCheckedSmtp(smtplib.SMTP):
    """把「对端返回码」从 `smtplib` 的异常里取出来，其余异常继续上抛。

    标准库把对端的拒绝包装成几种异常形态：收件人被拒抛出
    :class:`smtplib.SMTPRecipientsRefused`（每个收件人一个码）、发件人被拒抛出
    :class:`smtplib.SMTPSenderRefused`、其余走
    :class:`smtplib.SMTPResponseException`，greeting 期还会抛
    :class:`smtplib.SMTPConnectError`。后三者本身带 ``smtp_code``；第一种
    **不带**，只能在抛出点从它携带的 ``recipients`` 字典里取。取码与分类因此
    收敛到 :meth:`_respond`，每个交互点各有一个**私有**包装方法
    （:meth:`_connect` / :meth:`_starttls` / :meth:`_login` / :meth:`_sendmail`，
    收尾用 :meth:`_quit`）。

    为什么用私有方法而不是覆写标准库方法：override 的返回类型要带上
    `SmtpDeliveryResult`，就不再是 typeshed 里 ``sendmail -> _SendErrs``、
    ``starttls`` / ``login -> _Reply`` 这些返回值的子类型，mypy 会报
    ``[override]``；为它再补一条类型抑制注释，等于把「类型标注必须完整」的门禁
    凿洞（`AGENTS.md` §7）。私有方法按 typeshed 逐参标注，调用方只走唯一的公开
    入口 :meth:`deliver`：全仓不留抑制注释，也没有 LSP 违例。

    对端每一次回复的原始文本都在这里被丢弃（只保留数字返回码），所以包装
    方法里不再出现 ``errmsg`` / ``recipients`` 的文本部分（`design.md` §2.4）。
    """

    def deliver(self, config: SmtpConfig, message: EmailMessage) -> SmtpDeliveryResult:
        """一次投递的完整序列：连接 →（按需）STARTTLS →（有登录名时）认证 → 投递。

        这是本类唯一的公开入口。序列中的每一步都可能被对端拒绝，拒绝一律由
        :meth:`_respond` 转成受控判定并**短路**返回，后面的步骤不再执行；收尾
        （``QUIT`` + 关连接）无论走哪条路径都必须在 ``finally`` 上做
        （`docs/code-style.md` §1.4），:meth:`_quit` 的返回值被丢弃——判定已经由
        ``return`` 定下，``QUIT`` 的返回码对这一次投递的结果没有影响；
        :meth:`_quit` 里抛出的异常仍按原样上抛，由调用方归入 I/O 故障。

        连接显式建（先空构造、再 :meth:`_connect`）：超时上界只能在构造时注入
        （`smtplib.SMTP.connect` 没有超时形参），而 greeting 期的对端拒绝因此带得
        到返回码，与「连接根本没建立起来」的 ``OSError`` 区分开（`design.md` §2.4）。

        序列留在本类内、不让调用方逐步驱动私有方法：把 :meth:`_connect` 这样的
        私有方法交给别的类按顺序调，等于把「时序」这块行为从本类搬走，也让
        「连接一定会被释放」无法在本类内保证。
        """
        try:
            connected = self._connect(config.host, config.port)
            if isinstance(connected, SmtpDeliveryResult):
                return connected
            if config.starttls:
                upgraded = self._starttls()
                if isinstance(upgraded, SmtpDeliveryResult):
                    return upgraded
            if config.username:
                authenticated = self._login(config.username, config.password)
                if isinstance(authenticated, SmtpDeliveryResult):
                    return authenticated
            # 显式给信封发件人（`MAIL FROM`）= 注入的配置值，不让它从报文头
            # 推断；收件人交给标准库从 To 头取。
            sent = self._sendmail(message, from_addr=config.mail_from)
        finally:
            self._quit()
        return sent if isinstance(sent, SmtpDeliveryResult) else accepted()

    def _respond(self, code: int) -> SmtpDeliveryResult:
        """把对端返回码按 `constants.py` 的区间常量分类成受控判定。"""
        return rejected(code=code, error_class=_classify_smtp_code(code))

    def _connect(self, host: str, port: int) -> tuple[int, bytes] | SmtpDeliveryResult:
        """建立连接；超时上界由构造时注入的 ``timeout`` 承担。

        `smtplib.SMTP.connect` 没有超时形参，所以构造时就带上 ``timeout``。
        greeting 期被拒（``greeting != 220``）时标准库抛
        :class:`smtplib.SMTPConnectError`——它带 ``smtp_code``（``421`` /
        ``554`` 等），这里按返回码给出受控判定，不让它落进「连接类故障
        ``code=None``」那一行（`design.md` §2.4 要记该次返回码）。
        """
        try:
            return super().connect(host, port)
        except smtplib.SMTPConnectError as refusal:
            return self._respond(refusal.smtp_code)

    def _starttls(self) -> tuple[int, bytes] | SmtpDeliveryResult:
        """STARTTLS 被拒同样是一个对端返回码。"""
        try:
            return super().starttls()
        except smtplib.SMTPResponseException as refusal:
            return self._respond(refusal.smtp_code)

    def _login(self, user: str, password: str) -> tuple[int, bytes] | SmtpDeliveryResult:
        """认证失败通常是 ``535``，同样按返回码分类（口令只进标准库，不进结果）。"""
        try:
            return super().login(user, password)
        except smtplib.SMTPResponseException as refusal:
            return self._respond(refusal.smtp_code)

    def _sendmail(
        self, message: EmailMessage, *, from_addr: str
    ) -> dict[str, tuple[int, bytes]] | SmtpDeliveryResult:
        """投递报文：把 ``sendmail`` 层的三种拒绝形态转成受控判定。

        走标准库 `SMTP.send_message`——它负责按报文头展开收件人（`To` /
        `Cc` / `Bcc`）、拍平 MIME 并按报文决定 `SMTPUTF8` / `8BITMIME`；
        这些都不重实现。信封发件人显式给 ``from_addr``（注入的服务配置），
        不让库从报文头推断（`AGENTS.md` §8 第 3 条）。
        """
        try:
            return super().send_message(message, from_addr=from_addr)
        except smtplib.SMTPRecipientsRefused as refusal:
            # recipients 形如 {地址: (码, 回复文本)}；只取码，文本丢弃。
            return self._respond(min(code for code, _ in refusal.recipients.values()))
        except smtplib.SMTPSenderRefused as refusal:
            return self._respond(refusal.smtp_code)
        except smtplib.SMTPResponseException as refusal:
            return self._respond(refusal.smtp_code)

    def _quit(self) -> SmtpDeliveryResult | None:
        """收尾：发 ``QUIT``，按标准库 `SMTP.__exit__` 的口径判它，再关连接。

        对端不是 ``221`` 时按返回码给出受控判定；连接早已断开
        （:class:`smtplib.SMTPServerDisconnected`，``OSError`` 子类）不算投递
        失败，照常关闭并返回 ``None``。返回 ``None`` 表示收尾本身没有可分类的
        返回码。无论走哪条路径都用 ``finally`` 关掉连接（`docs/code-style.md`
        §1.4）；关闭自身抛出的异常按原样上抛，由调用方归入 I/O 故障。
        """
        try:
            code, _ = super().quit()
        except smtplib.SMTPServerDisconnected:
            return None
        except smtplib.SMTPResponseException as refusal:
            return self._respond(refusal.smtp_code)
        finally:
            super().close()
        if code == 221:
            return None
        return self._respond(code)


class SmtplibSender:
    """用标准库 `smtplib` 投递邮件，经 `asyncio.to_thread` 调用。

    构造参数就是配置数据类 `SmtpConfig`——由调用方从配置读好注入（`design.md`
    §4.5）：

    ::

        SmtplibSender(SmtpConfig(host=..., port=..., username=..., password=...,
                                 starttls=..., timeout_seconds=..., mail_from=...,
                                 reply_to=...))

    本类不读环境变量、不 import `config`——「装配」发生在阶段 4 的 `flows/`。
    """

    def __init__(self, config: SmtpConfig) -> None:
        self._config = config

    async def send_message(
        self, message: OutboundMessage, *, timeout_seconds: int | None = None
    ) -> SmtpDeliveryResult:
        """投递一封邮件，返回受控判定；见接口 `providers/smtp/base.py` 的契约。

        ``timeout_seconds`` 缺省时用构造时注入的 `SMTP_TIMEOUT_SECONDS` 值。
        故障路径只有两条，都归 transient 并给出 ``code=None``：墙钟上界
        （`TimeoutError`）与连接 / 协议层 I/O 故障（`OSError`，`SMTPException`
        亦是其子类）。对端**有**返回码的拒绝由 `_ResponseCheckedSmtp.deliver` 的序列
        取码并分类（含 greeting 期），`5xx` 因此是 permanent；区间外的
        返回码同样给出受控判定，不从本方法抛出异常。
        """
        config = self._config
        timeout = timeout_seconds if timeout_seconds is not None else config.timeout_seconds
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._send_blocking, config, message, timeout),
                timeout,
            )
        except TimeoutError:
            # 墙钟上界到点：本次尝试按临时故障计（design.md §3.3「4xx 与连接 /
            # 超时类故障」一行）。线程本身无法取消，只能弃用它的结果。
            return rejected(code=None, error_class="transient")
        except OSError:
            # 连接被拒 / 域名解析失败 / 套接字读写超时等 I/O 故障，同样按临时
            # 故障计；具体原因不落库，也不进返回值（design.md §2.4）。
            return rejected(code=None, error_class="transient")

    def _send_blocking(
        self, config: SmtpConfig, message: OutboundMessage, timeout: int
    ) -> SmtpDeliveryResult:
        """同步投递（由 `asyncio.to_thread` 在线程里执行）：建会话、把序列交给它。

        连接的生命周期由 :meth:`_ResponseCheckedSmtp.deliver` 在它自己的
        ``finally`` 上收（`docs/code-style.md` §1.4），本方法只把注入的配置与
        构造好的报文递过去——序列、取码与分类都在会话类内，不在这里摊开。
        """
        client = _ResponseCheckedSmtp(timeout=timeout)
        return client.deliver(config, self._build_message(config, message))

    def _build_message(self, config: SmtpConfig, message: OutboundMessage) -> EmailMessage:
        """按中性内容构造报文；`From` 与 `Reply-To` 在此显式落笔。"""
        mime = EmailMessage()
        mime["From"] = config.mail_from
        mime["To"] = message.recipient
        mime["Reply-To"] = config.reply_to
        mime["Subject"] = message.subject
        mime.set_content(message.body, subtype=_MIME_SUBTYPES[message.body_format])
        return mime
