#!/usr/bin/env python3
"""直连 TLS 的配置层：模式、端口、校验上下文、启动校验与「关闭校验必须留痕」。

期望值出处（不取被测实现）：

* `specs/0002-smtp-imap-implicit-tls/spec.md` — `FR-1`（二态模式 + 端口 465 / 993）、
  `FR-6`（默认校验 / `TLS_VERIFY` / `TLS_CA_BUNDLE` / 启动告警）、`FR-7`（CA 不可读即拒启动）；
* `specs/0002-smtp-imap-implicit-tls/design.md` §3 — 上下文构建顺序与失败语义；
* `docs/research/04-security-and-review.md` — 关闭校验必须可观察。

TLS 上下文与证书的判据一律对着**仓库里提交的测试 CA**（`tests/fixtures/tls/ca.pem`）验证：
「校验开」必须能把测试 CA 装进信任库、校验关必须是 `CERT_NONE`——两条都读上下文自身的
属性，不由 `build_tls_context` 反推。
"""

from __future__ import annotations

import logging
import re
import ssl
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from vs_email_forwarding.config import Settings, build_tls_context, validate_startup
from vs_email_forwarding.providers.imap.imapclient_fetcher import ImapConfig
from vs_email_forwarding.providers.smtp.smtplib_sender import SmtpConfig


def _repository_root() -> Path:
    """锚定真实仓库根：从本文件向上找到 `.git`。

    不能按固定层数回推——mutmut 把工程复制到 `mutants/` 下运行，固定层数会把扫描
    对象换成变异体（口径同 `tests/unit/test_config_hygiene.py::_repository_root`）。
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").exists():
            return parent
    raise AssertionError("测试不在仓库内运行：向上未找到 .git")


_REPO_ROOT = _repository_root()
_SRC = _REPO_ROOT / "src" / "vs_email_forwarding"
_TESTS = _REPO_ROOT / "tests"
_TEST_CA = _REPO_ROOT / "tests" / "fixtures" / "tls" / "ca.pem"

#: 合法三元组（启动校验的其余三条判据都成立）。
_MAIL_FROM: Final = "noreply@example.com"
_REPLY_TO: Final = "reply@example.com"

#: 会被环境变量覆盖的配置项：用例自己给值，先剔掉同名环境变量（`docs/testing.md` R12）。
_ENV_KEYS: Final = (
    "MAIL_FROM_ADDRESS",
    "REPLY_TO_ADDRESS",
    "IMAP_ADDRESS",
    "SMTP_TLS_MODE",
    "IMAP_TLS_MODE",
    "SMTP_PORT",
    "IMAP_PORT",
    "TLS_VERIFY",
    "TLS_CA_BUNDLE",
)


def _settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Settings:
    """只认用例显式给值的配置对象（关掉 `.env`、剔掉同名环境变量）。"""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    values: dict[str, Any] = {
        "mail_from_address": _MAIL_FROM,
        "reply_to_address": _REPLY_TO,
        "imap_address": _REPLY_TO,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_defaults_are_implicit_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    """`FR-1`：默认 `ssl`，端口 465 / 993；校验默认开、CA bundle 默认空。"""
    settings = _settings(monkeypatch)
    assert settings.smtp_tls_mode == "ssl"
    assert settings.imap_tls_mode == "ssl"
    assert settings.smtp_port == 465
    assert settings.imap_port == 993
    assert settings.tls_verify is True
    assert settings.tls_ca_bundle == ""


def test_starttls_keys_are_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """`FR-1`：两个 STARTTLS 配置项被删除（不是改名保留）。"""
    fields = Settings.model_fields
    assert "smtp_starttls" not in fields
    assert "imap_starttls" not in fields
    assert "smtp_tls_mode" in fields
    assert "imap_tls_mode" in fields


@pytest.mark.parametrize("field", ["smtp_tls_mode", "imap_tls_mode"])
def test_tls_mode_rejects_unknown_values(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    """`FR-1`：模式取值只有 `plain` / `ssl`，写错在构造配置时就报错。"""
    with pytest.raises(ValidationError):
        _settings(monkeypatch, **{field: "starttls"})


def test_validate_startup_rejects_unreadable_ca_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`FR-7`：CA bundle 非空但不可读 → 拒绝启动，信息只出现配置项名。"""
    missing = tmp_path / "no-such-ca.pem"
    with pytest.raises(ValueError) as excinfo:
        validate_startup(_settings(monkeypatch, tls_ca_bundle=str(missing)))

    message = str(excinfo.value)
    assert "TLS_CA_BUNDLE" in message
    assert str(missing) not in message


def test_validate_startup_accepts_readable_ca_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`FR-7` 的边界：读得到就不拦（内容合法性留给 TLS 握手，不在启动时猜）。"""
    readable = tmp_path / "ca.pem"
    readable.write_bytes(_TEST_CA.read_bytes())

    validate_startup(_settings(monkeypatch, tls_ca_bundle=str(readable)))


def test_build_tls_context_verifies_with_custom_ca(monkeypatch: pytest.MonkeyPatch) -> None:
    """`FR-6`：默认校验链与主机名，且 `TLS_CA_BUNDLE` 真的进了信任库。"""
    context = build_tls_context(_settings(monkeypatch, tls_ca_bundle=str(_TEST_CA)))

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    subjects = [str(cert.get("subject", "")) for cert in context.get_ca_certs()]
    assert any("vs-email-forwarding tests CA" in subject for subject in subjects)


def test_build_tls_context_disables_verification_but_keeps_encryption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`FR-6`：`TLS_VERIFY=false` = 跳过对端身份校验（不是关掉加密、也不是用私有 API）。"""
    context = build_tls_context(_settings(monkeypatch, tls_verify=False))

    assert context.verify_mode == ssl.CERT_NONE
    assert context.check_hostname is False


def test_build_tls_context_uses_system_store_without_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`FR-6`：不配 CA bundle 时用运行环境默认信任库（不是空信任库）。"""
    context = build_tls_context(_settings(monkeypatch))

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.get_ca_certs()


def test_smtp_config_requires_context_in_ssl_mode() -> None:
    """`design.md` §4.1：`ssl` 模式漏传上下文是契约违规，构造时就报。"""
    with pytest.raises(ValueError, match="ssl_context"):
        SmtpConfig(
            host="127.0.0.1",
            port=465,
            username="",
            password="",
            tls_mode="ssl",
            ssl_context=None,
            timeout_seconds=10,
            mail_from=_MAIL_FROM,
            reply_to=_REPLY_TO,
        )


def test_smtp_config_plain_mode_needs_no_context() -> None:
    """`plain` 模式的合法形态：没有上下文、也不去构建它。"""
    config = SmtpConfig(
        host="127.0.0.1",
        port=3025,
        username="",
        password="",
        tls_mode="plain",
        ssl_context=None,
        timeout_seconds=10,
        mail_from=_MAIL_FROM,
        reply_to=_REPLY_TO,
    )

    assert config.tls_mode == "plain"


def test_imap_config_requires_context_in_ssl_mode() -> None:
    """`design.md` §4.2：IMAP 侧同一条契约。"""
    with pytest.raises(ValueError, match="ssl_context"):
        ImapConfig(
            host="127.0.0.1",
            port=993,
            username="",
            password="",
            mailbox="INBOX",
            tls_mode="ssl",
            ssl_context=None,
            timeout_seconds=10,
        )


def test_imap_config_plain_mode_needs_no_context() -> None:
    """`plain` 模式：没有上下文。"""
    config = ImapConfig(
        host="127.0.0.1",
        port=3143,
        username="",
        password="",
        mailbox="INBOX",
        tls_mode="plain",
        ssl_context=None,
        timeout_seconds=10,
    )

    assert config.tls_mode == "plain"


def test_sources_have_no_starttls_upgrade_call() -> None:
    """`AC-3` / `AC-5`：`src/` 与 `tests/` 里没有 STARTTLS 升级的调用或参数。

    只认**可执行形态**（方法调用、关键字参数、字段声明）：注释里「STARTTLS 已移除」
    这句说明本身不是调用路径。
    """
    pattern = re.compile(r"(?i)\bstarttls\s*[\(=:]")
    offenders = [
        str(path.relative_to(_REPO_ROOT))
        for base in (_SRC, _TESTS)
        for path in base.rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


_FILES_WITHOUT_LEGACY_KEYS: Final = (
    ".env.example",
    "docs/configuration.md",
    "compose.dev.yaml",
)


@pytest.mark.parametrize("token", ["SMTP_STARTTLS", "IMAP_STARTTLS", "993 不要填"])
def test_legacy_keys_and_warning_are_removed(token: str) -> None:
    """`AC-9`：旧键与「993 不要填」的口径在示例与配置说明里都不再出现。"""
    for relative in _FILES_WITHOUT_LEGACY_KEYS:
        text = (_REPO_ROOT / relative).read_text(encoding="utf-8")
        assert token not in text, f"{relative} 仍含 {token}"


def test_env_example_documents_implicit_tls_examples() -> None:
    """`FR-9` / `AC-9`：示例口径是 465 / 993 + `ssl`，本地段是 GreenMail 明文口 + `plain`。"""
    text = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "SMTP_PORT=465" in text
    assert "IMAP_PORT=993" in text
    assert "SMTP_TLS_MODE=ssl" in text
    assert "IMAP_TLS_MODE=ssl" in text
    assert "SMTP_TLS_MODE=plain" in text
    assert "IMAP_TLS_MODE=plain" in text


async def _run_lifespan(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    """跑一次 lifespan（协作者全换成假件，不建引擎、不起真任务、不连库）。"""
    from vs_email_forwarding import main

    async def _fake_dispose() -> None:
        return None

    class _FakeScheduler:
        def __init__(self, received: Settings, ticks: dict[str, Any]) -> None:
            self._settings = received
            self._ticks = ticks

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "validate_startup", lambda received: None)
    monkeypatch.setattr(main, "get_engine", lambda: object())
    monkeypatch.setattr(main, "session_factory", lambda: "session-factory")
    monkeypatch.setattr(main, "dispose_engine", _fake_dispose)
    monkeypatch.setattr(main, "Scheduler", _FakeScheduler)

    async with main.lifespan(FastAPI()):
        pass


async def test_lifespan_warns_when_tls_verification_is_disabled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`FR-6` / `AC-7`：关闭校验是允许的，但启动必须留下可观察的结构化告警。"""
    caplog.set_level(logging.WARNING)
    await _run_lifespan(monkeypatch, _settings(monkeypatch, tls_verify=False))

    assert "tls_verify_disabled" in caplog.text
    assert "example.com" not in caplog.text


async def test_lifespan_stays_silent_when_tls_verification_is_enabled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`FR-6` 的另一面：校验开着时没有这条告警（否则告警就没有意义）。"""
    caplog.set_level(logging.WARNING)
    await _run_lifespan(monkeypatch, _settings(monkeypatch))

    assert "tls_verify_disabled" not in caplog.text


def test_flows_wire_tls_mode_and_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """`FR-1` / `FR-2` / `FR-4`：装配点逐字段注入配置，`ssl` 模式注入同一份校验上下文。"""
    from vs_email_forwarding.flows.fetch_inbound import _build_fetcher
    from vs_email_forwarding.flows.send_email import _build_sender

    settings = _settings(
        monkeypatch,
        smtp_host="smtp.example.invalid",
        smtp_port=2465,
        smtp_username="smtp-user",
        smtp_password="smtp-pass-placeholder",
        smtp_timeout_seconds=7,
        imap_host="imap.example.invalid",
        imap_port=2993,
        imap_username="imap-user",
        imap_password="imap-pass-placeholder",
        imap_mailbox="Review",
        imap_timeout_seconds=9,
        smtp_tls_mode="ssl",
        imap_tls_mode="ssl",
        tls_ca_bundle=str(_TEST_CA),
    )

    sender = _build_sender(settings)
    fetcher = _build_fetcher(settings)

    smtp_config = sender._config
    assert smtp_config.host == "smtp.example.invalid"
    assert smtp_config.port == 2465
    assert smtp_config.username == "smtp-user"
    assert smtp_config.password == "smtp-pass-placeholder"
    assert smtp_config.timeout_seconds == 7
    assert smtp_config.mail_from == _MAIL_FROM
    assert smtp_config.reply_to == _REPLY_TO
    assert smtp_config.tls_mode == "ssl"
    # 同一份上下文（缓存命中）：装配点不许自己再造一个、也不许退回默认上下文。
    assert smtp_config.ssl_context is build_tls_context(settings)
    assert smtp_config.ssl_context is not None
    assert smtp_config.ssl_context.verify_mode == ssl.CERT_REQUIRED

    imap_config = fetcher._config
    assert imap_config.host == "imap.example.invalid"
    assert imap_config.port == 2993
    assert imap_config.username == "imap-user"
    assert imap_config.password == "imap-pass-placeholder"
    assert imap_config.mailbox == "Review"
    assert imap_config.timeout_seconds == 9
    assert imap_config.tls_mode == "ssl"
    assert imap_config.ssl_context is build_tls_context(settings)
    assert imap_config.ssl_context is not None
    assert imap_config.ssl_context.verify_mode == ssl.CERT_REQUIRED


def test_build_tls_context_defaults_to_cached_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """`FR-6`：不传参时用 `get_settings()` 的那份配置（不是内建默认值、也不是空上下文）。"""
    from vs_email_forwarding import config as config_module

    settings = _settings(monkeypatch, tls_ca_bundle=str(_TEST_CA))
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)

    context = build_tls_context()

    assert context is build_tls_context(settings)
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_validate_startup_defaults_to_cached_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """`FR-7`：不传参时校验的是 `get_settings()` 的那份配置。"""
    from vs_email_forwarding import config as config_module

    settings = _settings(monkeypatch, imap_address="other@example.com")
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)

    with pytest.raises(ValueError, match="IMAP_ADDRESS"):
        validate_startup()


def test_flows_skip_context_in_plain_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """`FR-3` / `FR-5`：`plain` 模式不构建上下文（也不碰 CA 文件）。"""
    from vs_email_forwarding.flows.fetch_inbound import _build_fetcher
    from vs_email_forwarding.flows.send_email import _build_sender

    settings = _settings(
        monkeypatch,
        smtp_host="smtp.example.invalid",
        smtp_username="smtp-user",
        smtp_password="smtp-pass-placeholder",
        imap_host="imap.example.invalid",
        imap_username="imap-user",
        imap_password="imap-pass-placeholder",
        smtp_tls_mode="plain",
        imap_tls_mode="plain",
        # 故意指向不存在的 CA：plain 模式不该去读它。
        tls_ca_bundle="/nonexistent/ca.pem",
    )

    assert _build_sender(settings)._config.ssl_context is None
    assert _build_fetcher(settings)._config.ssl_context is None
