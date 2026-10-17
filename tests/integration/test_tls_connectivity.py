#!/usr/bin/env python3
"""直连 TLS（隐式 TLS）的端到端用例：SMTP 465 / IMAP 993 两个场景（spec 0002）。

替身与真实现的分工（`docs/testing.md` §4.2）：这里不 mock 任何本服务代码——投递仍走生产适配器
`SmtplibSender` + 标准库 `smtplib`，拉取仍走生产适配器 `ImapClientFetcher` + `IMAPClient`；替身
只在**进程边界**上，且是真的 TLS 服务端（`tests/integration/conftest.py` 的 `smtp_tls_server` /
`imap_tls_server`，证书由 `tests/fixtures/tls/` 的测试 CA 签发）。因此用例断言的是线上字节与真实
握手，而不是「调用没抛异常」。

期望值出处：`specs/0002-smtp-imap-implicit-tls/spec.md` 的 `AC-2` / `AC-4` / `AC-6` / `AC-7` /
`FR-8`（失败归类为连接类临时故障）；TLS 校验语义见 `docs/research/04-security-and-review.md`。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from vs_email_forwarding.config import Settings, build_tls_context
from vs_email_forwarding.providers.imap.imapclient_fetcher import ImapClientFetcher, ImapConfig
from vs_email_forwarding.providers.smtp.base import OutboundMessage
from vs_email_forwarding.providers.smtp.smtplib_sender import SmtpConfig, SmtplibSender

pytestmark = pytest.mark.integration

_MAIL_FROM = "noreply@example.com"
_REPLY_TO = "reply@example.com"
_RECIPIENT = "to@example.com"


def _settings(*, tls_ca_bundle: str = "", tls_verify: bool = True) -> Settings:
    """只给 TLS 相关项的配置对象（关掉 `.env`，其余取默认值）。"""
    return Settings(
        _env_file=None,
        tls_verify=tls_verify,
        tls_ca_bundle=tls_ca_bundle,
    )


def _smtp_sender(port: int, *, context: Any, tls_mode: str = "ssl") -> SmtplibSender:
    return SmtplibSender(
        SmtpConfig(
            host="127.0.0.1",
            port=port,
            username="",
            password="",
            tls_mode=tls_mode,
            ssl_context=context,
            timeout_seconds=10,
            mail_from=_MAIL_FROM,
            reply_to=_REPLY_TO,
        )
    )


def _message() -> OutboundMessage:
    return OutboundMessage(
        recipient=_RECIPIENT, subject="TLS 冒烟", body="hello over tls", body_format="text"
    )


async def test_smtp_ssl_delivers_over_real_tls_with_configured_ca(
    smtp_tls_server: Any, tls_ca_bundle: Path
) -> None:
    """`AC-2` / `AC-6`：`ssl` 模式建连即 TLS，`TLS_CA_BUNDLE` 生效后可投递。"""
    smtp_tls_server.reset()
    sender = _smtp_sender(
        smtp_tls_server.port,
        context=build_tls_context(_settings(tls_ca_bundle=str(tls_ca_bundle))),
    )

    result = await sender.send_message(_message())

    assert result.accepted is True
    assert result.error_class is None
    assert smtp_tls_server.delivery_count == 1
    captured = smtp_tls_server.deliveries[0]
    assert captured.mail_from == _MAIL_FROM


async def test_smtp_ssl_rejects_untrusted_certificate_as_transient(
    smtp_tls_server: Any,
) -> None:
    """`AC-2` / `AC-6`：默认信任库不含测试 CA ⇒ 握手失败，归连接类 transient、不投递。"""
    smtp_tls_server.reset()
    sender = _smtp_sender(smtp_tls_server.port, context=build_tls_context(_settings()))

    result = await sender.send_message(_message())

    assert result.accepted is False
    assert result.code is None
    assert result.error_class == "transient"
    assert smtp_tls_server.delivery_count == 0


async def test_smtp_ssl_with_verification_disabled_still_encrypts(smtp_tls_server: Any) -> None:
    """`AC-7`：`TLS_VERIFY=false` 时不受信任的证书也能投递（加密仍然生效）。"""
    smtp_tls_server.reset()
    sender = _smtp_sender(
        smtp_tls_server.port, context=build_tls_context(_settings(tls_verify=False))
    )

    result = await sender.send_message(_message())

    assert result.accepted is True
    assert smtp_tls_server.delivery_count == 1


async def test_smtp_ssl_against_plaintext_port_is_transient(smtp_server: Any) -> None:
    """`FR-8`：`ssl` 模式误连明文端口（或反之）不得把异常抛给调用方，按 transient 记。"""
    smtp_server.reset()
    sender = _smtp_sender(smtp_server.port, context=build_tls_context(_settings(tls_ca_bundle="")))

    result = await sender.send_message(_message())

    assert result.accepted is False
    assert result.code is None
    assert result.error_class == "transient"
    assert smtp_server.delivery_count == 0


async def test_smtp_plain_mode_still_delivers(smtp_server: Any) -> None:
    """`AC-3`：保留的 `plain` 模式（本地 GreenMail 口径）仍能投递。"""
    smtp_server.reset()
    sender = _smtp_sender(smtp_server.port, context=None, tls_mode="plain")

    result = await sender.send_message(_message())

    assert result.accepted is True
    assert smtp_server.delivery_count == 1


def _imap_fetcher(port: int, *, context: Any) -> ImapClientFetcher:
    return ImapClientFetcher(
        ImapConfig(
            host="127.0.0.1",
            port=port,
            username="tester",
            password="placeholder",
            mailbox="INBOX",
            tls_mode="ssl",
            ssl_context=context,
            timeout_seconds=10,
        )
    )


async def test_imap_ssl_reads_state_and_fetches(imap_tls_capture: Any, tls_ca_bundle: Path) -> None:
    """`AC-4` / `AC-6`：`ssl` 模式完成 SELECT + UID FETCH，且会话里没有 STARTTLS。"""
    fetcher = _imap_fetcher(
        imap_tls_capture.port,
        context=build_tls_context(_settings(tls_ca_bundle=str(tls_ca_bundle))),
    )

    state = await fetcher.read_mailbox_state()
    assert state.error_class is None
    assert state.state is not None
    assert state.state.uidvalidity == 1000
    assert state.state.uidnext == 2

    fetched = await fetcher.fetch_messages(after_uid=0, limit=10)
    assert fetched.error_class is None
    assert len(fetched.messages) == 1
    message = fetched.messages[0]
    assert message.uid == 1
    assert message.mailbox == "INBOX"
    assert message.headers["subject"] == "Re: hello"
    assert message.parts
    assert message.parts[0].payload is not None
    assert message.parts[0].payload.strip() == "reply body"

    commands = [command.upper() for command in imap_tls_capture.commands]
    assert not any(command.startswith("STARTTLS") for command in commands)


async def test_imap_ssl_untrusted_certificate_is_transient(imap_tls_capture: Any) -> None:
    """`AC-4` / `FR-8`：不受信任的证书 ⇒ 受控 transient，不抛异常、不推进任何东西。"""
    with imap_tls_capture._lock:
        imap_tls_capture.commands = []
    fetcher = _imap_fetcher(imap_tls_capture.port, context=build_tls_context(_settings()))

    state = await fetcher.read_mailbox_state()
    fetched = await fetcher.fetch_messages(after_uid=0, limit=10)

    assert state.state is None
    assert state.error_class == "transient"
    assert fetched.messages == ()
    assert fetched.error_class == "transient"
    assert imap_tls_capture.commands == []
