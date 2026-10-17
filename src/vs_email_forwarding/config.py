#!/usr/bin/env python3
"""应用配置层。

本模块是**唯一**的配置入口：所有可变项都收敛在 ``.env``，业务代码必须通过
:func:`get_settings` 读取，禁止在别处直接访问环境变量或再读一次 ``.env``。
"""

from __future__ import annotations

import os
import ssl
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from vs_email_forwarding.core.message_id import extract_domain

#: SMTP / IMAP 的连接形态：`ssl` = 建连即 TLS（隐式 TLS）；`plain` = 全程明文（本地联调）。
#: STARTTLS 已移除——连到只接受 STARTTLS 的端口会失败，这是有意的边界。
TlsMode = Literal["plain", "ssl"]


class Settings(BaseSettings):
    """服务配置。

    字段名与环境变量名（大写形式）一一对应，例如 ``app_env`` 对应 ``APP_ENV``。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 运行环境：dev 本地开发 / test 测试 / prod 生产。
    # 取值严格限定，写错会在启动时直接报错；三者与 .env.example 的说明保持一致。
    app_env: Literal["dev", "test", "prod"] = "dev"

    # 标准库 logging 的级别，例如 DEBUG / INFO / WARNING。
    log_level: str = "INFO"

    # 日志渲染格式：json 供生产采集，console 供本地阅读（带颜色）。
    log_format: Literal["json", "console"] = "console"

    # HTTP 服务监听地址与端口。
    http_host: str = "0.0.0.0"
    http_port: int = 8000

    # 数据库连接串（异步驱动）；四张表的表结构与迁移方案见
    # specs/0001-email-forwarding-service/design.md 的数据模型一节。
    database_url: str = (
        "postgresql+asyncpg://vs_email_forwarding:change-me@127.0.0.1:5432/vs_email_forwarding"
    )

    # 发件人地址与回复地址（Reply-To）：服务端发信时使用。
    mail_from_address: str = "noreply@example.com"
    reply_to_address: str = "reply@example.com"

    # SMTP 发信配置；真实主机与凭据由所有者后续提供，预研阶段允许留空。
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_username: str = ""
    smtp_password: str = ""
    # 连接形态：`ssl`（默认，465 隐式 TLS）/ `plain`（本地 GreenMail 明文口）。
    smtp_tls_mode: TlsMode = "ssl"

    # IMAP 收信配置；真实主机与凭据由所有者后续提供，预研阶段允许留空。
    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    imap_mailbox: str = "INBOX"
    # 连接形态：`ssl`（默认，993 隐式 TLS）/ `plain`（本地 GreenMail 明文口）。
    imap_tls_mode: TlsMode = "ssl"

    # 对外 TLS 连接是否校验证书链与主机名（SMTP / IMAP 共用）；默认校验。
    # 显式关闭时连接仍然加密、只是不验证对端身份，启动阶段会记一条结构化告警。
    tls_verify: bool = True

    # 私有 CA / 自签网关的 PEM 路径（SMTP / IMAP 共用）；空串 = 运行环境默认信任库。
    # 配置为非空但不可读时启动即拒绝（validate_startup 的第 4 条判据）。
    tls_ca_bundle: str = ""

    # 被拉取信箱**地址**（不是文件夹）：启动即校验，必须与 reply_to_address 相同；
    # 为空或不一致都拒绝启动。它**不默认取 imap_username**——登录名不保证是邮箱地址。
    imap_address: str = ""

    # 单次外部调用的墙钟上界（秒）；FR-15 的 3 分钟由它们与退避表共同推出。
    smtp_timeout_seconds: int = 10
    imap_timeout_seconds: int = 10
    review_timeout_seconds: int = 10

    # 每批条数，五条流共用；流③ 用它限制单个 tick 拉取的封数。
    batch_size: int = 100

    # 内容长度上限：API 层校验，超限返回 422 且不落库。
    # 主题单位是字符，正文单位是字节（单位写进字段名，避免再把 MiB 与字符搞混）。
    max_subject_chars: int = 500
    max_body_bytes: int = 1_048_576

    # 入站回复寄件人地址（外部可控的 `From`）入库前的**字符**上限：流③ 超长截断后再入库。
    # 与上面两项不同，它不参与 API 校验、不产生 422——挡的是「外部可控的头值不得把整封写不进去」
    # （design.md §3.4 的入站行取值表；所有者裁决，已否决「不设防」）。
    max_recipient_chars: int = 320

    # GET /v1/messages/{id}/replies 每页条数上限（keyset 游标分页）。
    replies_page_size: int = 100

    # 四条业务流的轮询间隔（秒）；10 秒是既定间隔，但不得在代码中写死，只能走配置。
    poll_review_outbound_seconds: int = 10
    poll_send_seconds: int = 10
    poll_fetch_inbound_seconds: int = 10
    poll_review_inbound_seconds: int = 10

    # 第 5 条后台任务（保留期清理）的执行间隔（秒）：清理不是时效敏感动作，默认按小时跑。
    poll_cleanup_seconds: int = 3600


@lru_cache
def get_settings() -> Settings:
    """返回进程内缓存的配置单例。"""
    return Settings()


def build_tls_context(settings: Settings | None = None) -> ssl.SSLContext:
    """按配置构建对外 TLS 连接用的 ``SSLContext``（SMTP 与 IMAP 共用）。

    - 默认**校验**证书链与主机名（``TLS_VERIFY=true``）；
    - ``TLS_VERIFY=false`` 时**仍然加密**，只是跳过对端身份校验——先关
      ``check_hostname`` 再设 ``verify_mode=CERT_NONE``（顺序反了标准库抛
      ``ValueError``）；
    - ``TLS_CA_BUNDLE`` 非空时以它为受信任 CA（私有 CA / 自签网关），否则用运行
      环境的默认信任库。

    上下文按 ``(verify, ca_bundle)`` 缓存：适配器每个 tick 装配一次，没必要反复加载
    系统证书。调用点在 ``flows/`` 的装配函数（``providers/`` 不读配置）。
    """
    resolved = get_settings() if settings is None else settings
    return _build_tls_context(resolved.tls_verify, resolved.tls_ca_bundle)


@lru_cache(maxsize=8)
def _build_tls_context(verify: bool, ca_bundle: str) -> ssl.SSLContext:
    """``build_tls_context`` 的缓存实现；参数必须可哈希。"""
    context = ssl.create_default_context(cafile=ca_bundle or None)
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def validate_startup(settings: Settings | None = None) -> None:
    """启动即校验：四条判据任一不满足就抛 ``ValueError``，拒绝启动（``design.md`` §4.4）。

    四条判据（一次性全部检查后统一报出，部署配置写错时一轮就能改完）：

    1. ``IMAP_ADDRESS`` 与 ``REPLY_TO_ADDRESS`` 相等且非空——两者不一致时回复回路是**静默断掉**
       的（邮件照发、回复永远拉不回来），这正是本设计已经否决过的隐式依赖；
    2. ``MAIL_FROM_ADDRESS`` 能解析出邮件域（取域函数在 :mod:`vs_email_forwarding.core.message_id`，
       出站 ``Message-ID`` 的域取自它）——``<uuid@>`` 这类残缺值必须在这里被拦住；
    3. ``REPLY_TO_ADDRESS`` 本身是合法地址（含 ``@``、无空白）；
    4. ``TLS_CA_BUNDLE`` 若配置为非空，则文件必须可读——否则第一次 TLS 握手才报错，
       而且会被归成临时故障、看不出根因。

    **调用点在 ``main.py`` 的 lifespan 显式启动步骤，不在 import 期**：多 worker 或 ``--reload``
    下，import 期异常会表现为「每个 worker 各拒一次」的无尽重启循环，而不是一条清晰的启动错误。

    异常信息里只出现**配置项名**，不出现地址本身（``NFR-5``、``AGENTS.md`` §8 第 4 条）。
    """
    resolved = get_settings() if settings is None else settings
    failures: list[str] = []
    if not resolved.imap_address.strip() or resolved.imap_address != resolved.reply_to_address:
        failures.append("IMAP_ADDRESS 必须与 REPLY_TO_ADDRESS 相同且非空")
    if extract_domain(resolved.mail_from_address) is None:
        failures.append("MAIL_FROM_ADDRESS 解析不出邮件域")
    if not _is_valid_address(resolved.reply_to_address):
        failures.append("REPLY_TO_ADDRESS 不是合法地址（须含 @ 且不含空白）")
    if resolved.tls_ca_bundle and not os.access(resolved.tls_ca_bundle, os.R_OK):
        failures.append("TLS_CA_BUNDLE 指向的文件不可读")
    if failures:
        raise ValueError("启动校验未通过：" + "；".join(failures))


def _is_valid_address(value: str) -> bool:
    """地址是否「含 ``@``、无空白」（``design.md`` §4.4 判据 3 的字面口径）。"""
    return "@" in value and not any(char.isspace() for char in value)


__all__ = ["Settings", "TlsMode", "build_tls_context", "get_settings", "validate_startup"]
