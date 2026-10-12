"""应用配置层。

本模块是**唯一**的配置入口：所有可变项都收敛在 ``.env``，业务代码必须通过
:func:`get_settings` 读取，禁止在别处直接访问环境变量或再读一次 ``.env``。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True

    # IMAP 收信配置；真实主机与凭据由所有者后续提供，预研阶段允许留空。
    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    imap_mailbox: str = "INBOX"

    # 被拉取信箱**地址**（不是文件夹）：启动即校验，必须与 reply_to_address 相同；
    # 为空或不一致都拒绝启动。它**不默认取 imap_username**——登录名不保证是邮箱地址。
    imap_address: str = ""

    # 入站拉取是否启用 STARTTLS。
    imap_starttls: bool = True

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


__all__ = ["Settings", "get_settings"]
