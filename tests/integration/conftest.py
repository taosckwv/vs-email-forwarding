#!/usr/bin/env python3
"""集成测试的共同夹具：真实 PostgreSQL 测试库 + 三条进程内替身 + 清表 + 容器未起整组跳过。

设计真源：`docs/testing.md` §4（集成测试的边界、替身位置、隔离与稳定性）、§8（标记与跳过）；
`specs/0001-email-forwarding-service/plan.md` §7.2（夹具清单）、§8.1（库隔离风险）。

七条口径（每一条都能被一条用例判定真假）：

1. **真实 PostgreSQL，不用 mock 数据库**（`docs/testing.md` §4.1）：抢占与事务用的是数据库
   语义，替身验证不了（理由见 `docs/research/02-runtime-and-scheduling.md`）。
2. **测试库按运行隔离**：库名是 ``vs_email_forwarding_test_<pid>_<随机 8 位>``，每次 pytest
   运行新建、结束删除；并行（含多 worker）不会撞。**开发库从不被连接**——本夹具只取配置里
   ``DATABASE_URL`` 的**服务器坐标**，库名一律由自己生成（`plan.md` §8.1 的串号风险在夹具侧
   兜住），且删库前先按命名约定再核一遍。
3. **建表走真实迁移**：``alembic -c migrations/alembic.ini upgrade head`` 对着隔离库跑一次，
   夹具因此同时验证「迁移能在真实 PostgreSQL 上跑通」。
4. **替身只在外部进程边界**（`docs/testing.md` §4.2、`plan.md` §7.2）：假 SMTP 是**进程内的
   真实 SMTP 协议服务器**（投递仍走生产适配器 `SmtplibSender` 与标准库 `smtplib`，`From` /
   `Reply-To` 因此是在线上字节上被验证的）；假 IMAP 与假审查是 `providers/` 接口的进程内实现，
   替换点在 `flows/fetch_inbound.py` 的装配函数与 `providers/review/factory.py` 的工厂。
   三条替身都不含业务判定——判定属 `core/`、`flows/` 与仓储层。
5. **用例间清表**：每个用例前后对隔离库做一次真实 `TRUNCATE`（表名取自 ORM 元数据）。
6. **容器未起时整组跳过**：收集钩子探测 PostgreSQL 可达性，不可达就把本目录的用例全部标记
   skip，并写明是哪一项依赖不可达（`docs/testing.md` §8：不允许静默跳过）。**跳过不等于通过**。
7. **用例之间不共享进程级状态**：`db/session.py` 的进程级单例引擎在每个用例前后被丢弃，
   事件循环因此不会被上一轮的连接池污染（`docs/testing.md` R12）。

连接串的来源只有两处：显式环境变量 ``VS_EMAIL_FORWARDING_TEST_DATABASE_URL``（若设置），
否则配置层的 ``DATABASE_URL``——**都只当服务器坐标用**，仓库里的配置文件一个字都不改
（`docs/testing.md` §4.3）。
"""

from __future__ import annotations

import asyncio
import contextlib
import email
import io
import os
import re
import socket
import subprocess
import sys
import threading
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from vs_email_forwarding.config import Settings, get_settings
from vs_email_forwarding.db import session as db_session
from vs_email_forwarding.db.models import Base
from vs_email_forwarding.flows import fetch_inbound
from vs_email_forwarding.providers.imap.base import (
    FetchedMessage,
    FetchedPart,
    FetchResult,
    ImapErrorClass,
    MailboxState,
    MailboxStateResult,
)
from vs_email_forwarding.providers.review import factory as review_factory
from vs_email_forwarding.providers.review.base import ReviewDecision, ReviewMessage

_REPO_ROOT = Path(__file__).resolve().parents[2]
_INTEGRATION_DIR = Path(__file__).resolve().parent

#: 显式指定「维护连接」的服务器坐标的测试专用环境变量（可选）。
_TEST_DATABASE_URL_ENV = "VS_EMAIL_FORWARDING_TEST_DATABASE_URL"

#: 测试库名前缀与完整形状；删除前的复核对的是这个形状（`plan.md` §8.1 的兜底断言）。
_TEST_DATABASE_PREFIX = "vs_email_forwarding_test"
_TEST_DATABASE_NAME = re.compile(rf"{_TEST_DATABASE_PREFIX}_\d+_[0-9a-f]{{8}}")

#: 建库 / 删库要连的维护库：官方 postgres 镜像里一定有 `postgres`。
_MAINTENANCE_DATABASE = "postgres"

_PROBE_TIMEOUT_SECONDS = 3
_MIGRATION_TIMEOUT_SECONDS = 120
_HANG_SECONDS = 30

#: 夹具写的占位地址一律用 `example.com`（`AGENTS.md` §8 第 4 条、`docs/testing.md` §4.7）。
_PLACEHOLDER_FROM = "noreply@example.com"
_PLACEHOLDER_REPLY_TO = "reply@example.com"


# ---------------------------------------------------------------------------
# 一、依赖探测与运行隔离的测试库
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DependencyStatus:
    """依赖可达性：不可达时 `reason` 就是用例跳过时打印的原因（必须写明是哪一项）。"""

    is_reachable: bool
    reason: str
    maintenance_database: str | None = None


@dataclass(frozen=True)
class TestDatabase:
    """本次 pytest 运行独占的真实 PostgreSQL 测试库。

    `name` 带进程与随机后缀（运行级隔离），`url` 是指向它的异步连接串——用例与夹具一律经
    `url` 取库，绝不使用配置里那个库名。

    `configured_database` 是覆盖前配置里的库名（本地 `.env` 的开发库，或显式测试环境变量里的
    库名）：它**只用于让用例断言隔离真的生效**（`name != configured_database`），夹具从不连接它。
    """

    name: str
    url: str
    maintenance_database: str
    configured_database: str | None


def _maintenance_url() -> URL:
    """维护连接串：只取服务器坐标，库名由夹具自己决定。

    优先级：显式测试环境变量 ``VS_EMAIL_FORWARDING_TEST_DATABASE_URL`` → 配置层的
    ``DATABASE_URL``（`config.get_settings()`，读环境变量与 `.env`）。

    两种来源都**只用** host / port / user / password：配置里的库名（本地 `.env` 指向开发库）
    从头到尾不会被连接、清表或删除——这正是 `plan.md` §8.1 的「测试用库与开发库串号」风险
    在夹具一侧的兜底。
    """
    explicit = os.environ.get(_TEST_DATABASE_URL_ENV)
    if explicit:
        return make_url(explicit)
    return make_url(get_settings().database_url)


def _maintenance_databases(url: URL) -> tuple[str, ...]:
    """候选维护库：`postgres` 优先，必要时退回配置里那个库（仍然只当维护连接用）。"""
    configured = url.database
    if configured and configured != _MAINTENANCE_DATABASE:
        return (_MAINTENANCE_DATABASE, configured)
    return (_MAINTENANCE_DATABASE,)


def _connection_kwargs(url: URL, database: str) -> dict[str, object]:
    """`asyncpg.connect` 的关键字参数（用关键字而不是拼接 DSN，避开口令里的 URL 特殊字符）。"""
    return {
        "host": url.host or "127.0.0.1",
        "port": url.port or 5432,
        "user": url.username,
        "password": url.password,
        "database": database,
        "timeout": _PROBE_TIMEOUT_SECONDS,
    }


async def _probe_database(url: URL, database: str) -> str | None:
    """试连一个库做一次往返；成功返回 ``None``，失败返回受控的失败描述。

    连接建立后一律经 `try/finally` 关闭（与 `_create_database` / `_drop_database` 同一形状）：
    `close()` 自己出问题时不留下泄漏的连接。只有**建连**那一步的失败被转成受控描述（调用方
    据此判定「依赖不可达」并整组跳过）；`SELECT 1` 与 `close()` 的异常照常向上冒泡——`try/finally`
    只保证不漏掉关闭，不吞异常。
    """
    try:
        connection = await asyncpg.connect(**_connection_kwargs(url, database))
    except (OSError, TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
        return f"{type(exc).__name__}: {exc}"
    try:
        await connection.execute("SELECT 1")  # 一次往返：握手成功不等于这个库真的可用
    finally:
        await connection.close()
    return None


@lru_cache(maxsize=1)
def _dependency_status() -> _DependencyStatus:
    """本次进程内只探测一次：PostgreSQL 是否可达（跳过判据的唯一来源）。"""
    url = _maintenance_url()
    target = f"{url.host or '127.0.0.1'}:{url.port or 5432}"
    failures: list[str] = []
    for database in _maintenance_databases(url):
        failure = asyncio.run(_probe_database(url, database))
        if failure is None:
            return _DependencyStatus(is_reachable=True, reason="", maintenance_database=database)
        failures.append(f"{database} 库不可达（{failure}）")
    reason = (
        f"集成测试依赖不可达，整组跳过：PostgreSQL {target} 连接失败——"
        + "；".join(failures)
        + "。先 `bash scripts/dev-up.sh` 起依赖；跳过不等于通过（docs/testing.md §6 第 7 条、§8）。"
    )
    return _DependencyStatus(is_reachable=False, reason=reason)


def _isolated_database_name() -> str:
    """本次运行独占的库名：进程号 + 随机后缀，并行运行不会撞。"""
    return f"{_TEST_DATABASE_PREFIX}_{os.getpid()}_{uuid.uuid4().hex[:8]}"


def _checked_test_database_name(name: str) -> str:
    """删库 / 建库前按命名约定复核：只允许操作 `vs_email_forwarding_test_*` 形状的库。"""
    if _TEST_DATABASE_NAME.fullmatch(name) is None:
        raise RuntimeError(f"拒绝操作不符合测试库命名约定的库：{name}")
    return name


async def _create_database(url: URL, database: str, name: str) -> None:
    """在维护连接上 `CREATE DATABASE`（不能用事务包裹，`asyncpg.execute` 默认自动提交）。"""
    connection = await asyncpg.connect(**_connection_kwargs(url, database))
    try:
        await connection.execute(f'CREATE DATABASE "{_checked_test_database_name(name)}"')
    finally:
        await connection.close()


async def _drop_database(url: URL, database: str, name: str) -> None:
    """踢掉残留连接后删掉隔离库，一次运行一个库、运行结束不留痕。"""
    connection = await asyncpg.connect(**_connection_kwargs(url, database))
    try:
        await connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            _checked_test_database_name(name),
        )
        await connection.execute(
            f'DROP DATABASE IF EXISTS "{_checked_test_database_name(name)}" WITH (FORCE)'
        )
    finally:
        await connection.close()


def _run_migrations(database_url: str) -> None:
    """对着隔离库跑 `alembic upgrade head`（子进程，避免 Alembic 的日志配置污染 `caplog`）。

    迁移外壳（`migrations/env.py`）的连接串只从配置层取，因此这里用环境变量
    `DATABASE_URL` 覆盖、不改仓库里的任何文件（`docs/testing.md` §4.3）。
    """
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "migrations/alembic.ini", "upgrade", "head"],
        cwd=_REPO_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=_MIGRATION_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout).strip().splitlines()[-8:]
        raise RuntimeError("测试库迁移失败（alembic upgrade head）：\n" + "\n".join(tail))


@pytest.fixture(scope="session")
def test_database() -> Iterator[TestDatabase]:
    """本次运行独占的真实 PostgreSQL 测试库：建库 → 跑迁移 → 运行结束删库。"""
    status = _dependency_status()
    if not status.is_reachable or status.maintenance_database is None:
        pytest.skip(status.reason)
    url = _maintenance_url()
    name = _checked_test_database_name(_isolated_database_name())
    isolated_url = url.set(drivername="postgresql+asyncpg", database=name)
    asyncio.run(_create_database(url, status.maintenance_database, name))
    try:
        _run_migrations(isolated_url.render_as_string(hide_password=False))
        yield TestDatabase(
            name=name,
            url=isolated_url.render_as_string(hide_password=False),
            maintenance_database=status.maintenance_database,
            configured_database=url.database,
        )
    finally:
        asyncio.run(_drop_database(url, status.maintenance_database, name))


@pytest.fixture(scope="session")
def schema(test_database: TestDatabase) -> TestDatabase:
    """`plan.md` §7.2 夹具表里的具名 `schema`（session 级）：**命名对齐，不建第二套路径**。

    表里给它的职责是「建库后 `alembic upgrade head` + 用例前后清表」。落地版把这三件事拆在
    `test_database`（会话级：建库 + 迁移 + 结束删库）与 `clean_tables`（autouse：逐用例前后
    `TRUNCATE`）里——职责全覆盖，只是切分与表名不同。本夹具只把这条既有链路**换个名字**交出去，
    让下游卡（`T-34`~`T-38`）能按 §7.2 的表名引用它；任何第二套建库 / 迁移路径都是缺陷。
    """
    return test_database


# ---------------------------------------------------------------------------
# 二、进程内假 SMTP：真实 SMTP 协议 + 捕获端
# ---------------------------------------------------------------------------

type _SmtpMode = Literal["accept", "transient", "permanent", "hang"]

_ACCEPT_REPLY = b"250 2.0.0 OK\r\n"
_TRANSIENT_REPLY = b"451 4.3.0 Try again later\r\n"
_PERMANENT_REPLY = b"550 5.1.1 User unknown\r\n"


@dataclass(frozen=True)
class CapturedDelivery:
    """一次被捕获的投递：信封发件人、信封收件人与**原始报文**。

    `raw` 是线上字节经 RFC 5321 §4.5.2 点去填充后的**原始报文**（接收方义务，见
    `_read_message`），因此对它做正文的字节级断言不需要再自己处理点填充。
    `header()` 读的是这些字节里真实存在的头，因此 `From` / `Reply-To` 这类身份硬约束
    （`AGENTS.md` §8 第 3 条）是在边界上被验证的，不是从被测实现的对象里取出来的。
    """

    mail_from: str
    recipients: tuple[str, ...]
    raw: bytes

    def header(self, name: str) -> str | None:
        """取实收报文里的某个头值；头不存在时返回 `None`。"""
        return email.message_from_bytes(self.raw).get(name)


class FakeSmtpServer:
    """进程内假 SMTP 服务：真实 SMTP 协议交互 + 捕获端（`plan.md` §7.2 的 `smtp_capture`）。

    只替代「外部邮件服务器」这一**进程边界**：投递仍由生产适配器 `SmtplibSender` 经标准库
    `smtplib` 走 TCP 完成，因此这里捕获到的就是线上真实字节。

    三种脚本化档位（`plan.md` §7.2 / §7.4）：`transient`（`4xx`，可重试）、`permanent`
    （`5xx`，直接终态）、`hang`（接受连接后一个字节都不返回，由调用方的墙钟上界收场）。
    """

    def __init__(self, *, host: str = "127.0.0.1") -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((host, 0))
        self._listener.listen(8)
        self._listener.settimeout(0.5)
        self.port = int(self._listener.getsockname()[1])
        self.mode: _SmtpMode = "accept"
        #: 用例脚本化的原始回复文本（`AC-30` 用它塞入收件人地址，验证它不落库、不进日志）。
        self.reply_text: str | None = None
        self.deliveries: list[CapturedDelivery] = []
        self.connection_count = 0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._connections: list[socket.socket] = []
        self._thread = threading.Thread(target=self._serve, name="fake-smtp", daemon=True)

    def start(self) -> None:
        """启动监听线程（会话级：整个运行共用一个临时端口）。"""
        self._thread.start()

    def stop(self) -> None:
        """停止监听并断开残留连接（会话结束时的收尾，不留线程与套接字）。"""
        self._stop.set()
        self._thread.join(timeout=5)
        with self._lock:
            connections = list(self._connections)
            self._connections.clear()
        for connection in connections:
            with contextlib.suppress(OSError):
                connection.close()
        self._listener.close()

    def reset(self) -> None:
        """回到「接受」档并清空捕获记录——每个用例都从干净的捕获端起步。"""
        self.mode = "accept"
        self.reply_text = None
        with self._lock:
            self.deliveries = []
            self.connection_count = 0

    @property
    def delivery_count(self) -> int:
        """捕获到的投递次数（`AC-6` / `AC-29` / `AC-37` 的「没有多投」断言用它）。"""
        with self._lock:
            return len(self.deliveries)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with self._lock:
                self.connection_count += 1
                self._connections.append(connection)
            threading.Thread(target=self._handle, args=(connection,), daemon=True).start()

    def _handle(self, connection: socket.socket) -> None:
        try:
            with connection:
                if self.mode == "hang":
                    # AC-40：接受连接后既不回码也不关连接，单次尝试只能由调用方的
                    # SMTP_TIMEOUT_SECONDS 收场。
                    self._stop.wait(timeout=_HANG_SECONDS)
                    return
                self._converse(connection)
        finally:
            with self._lock:
                if connection in self._connections:
                    self._connections.remove(connection)

    def _converse(self, connection: socket.socket) -> None:
        """一次 SMTP 会话：够用的命令集（EHLO / MAIL / RCPT / DATA / QUIT）。"""
        connection.sendall(b"220 fake-smtp ready\r\n")
        reader = connection.makefile("rb")
        mail_from = ""
        recipients: list[str] = []
        while True:
            line = reader.readline()
            if not line:
                return
            command = line.strip().upper()
            if command.startswith(b"EHLO") or command.startswith(b"HELO"):
                connection.sendall(b"250 fake-smtp\r\n")
            elif command.startswith(b"MAIL FROM:"):
                mail_from = _address_of(line)
                recipients = []
                connection.sendall(b"250 2.1.0 OK\r\n")
            elif command.startswith(b"RCPT TO:"):
                recipients.append(_address_of(line))
                connection.sendall(b"250 2.1.5 OK\r\n")
            elif command == b"DATA":
                connection.sendall(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                self._record(mail_from, recipients, _read_message(reader))
                connection.sendall(self._data_reply())
            elif command == b"QUIT":
                connection.sendall(b"221 2.0.0 Bye\r\n")
                return
            else:
                connection.sendall(b"250 2.0.0 OK\r\n")

    def _data_reply(self) -> bytes:
        if self.reply_text is not None:
            return self.reply_text.rstrip("\r\n").encode("utf-8", "replace") + b"\r\n"
        match self.mode:
            case "transient":
                return _TRANSIENT_REPLY
            case "permanent":
                return _PERMANENT_REPLY
            case _:
                return _ACCEPT_REPLY

    def _record(self, mail_from: str, recipients: Sequence[str], raw: bytes) -> None:
        with self._lock:
            self.deliveries.append(
                CapturedDelivery(mail_from=mail_from, recipients=tuple(recipients), raw=raw)
            )


def _address_of(command: bytes) -> str:
    """从 `MAIL FROM:<a@b>` / `RCPT TO:<a@b>` 里取地址（尖括号缺失时退回冒号后的整段）。"""
    decoded = command.decode("ascii", "replace")
    start = decoded.find("<")
    end = decoded.find(">", start + 1)
    if start != -1 and end != -1:
        return decoded[start + 1 : end]
    return decoded.split(":", 1)[-1].strip()


def _read_message(reader: io.BufferedReader) -> bytes:
    """读一段 DATA 负载，直到单独一行的 `.`（填充由 `smtplib` 负责、去填充由**本函数**负责）。

    接收方按 RFC 5321 §4.5.2 履行点去填充义务：发送方会把正文里以 `.` 开头的行前面再补一个
    点，这里逐行去掉那一个前导点。因此 `CapturedDelivery.raw` 是**原始报文**，对正文做字节级
    断言时不需要再自己处理（`T-35` 的正文字节断言依赖这一点）。
    """
    chunks: list[bytes] = []
    while True:
        line = reader.readline()
        if not line or line in (b".\r\n", b".\n"):
            return b"".join(chunks)
        chunks.append(line[1:] if line.startswith(b".") else line)


@pytest.fixture(scope="session")
def smtp_server() -> Iterator[FakeSmtpServer]:
    """会话级假 SMTP 服务：整个运行共用一个监听端口，用例之间只重置捕获记录。"""
    server = FakeSmtpServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def smtp_capture(smtp_server: FakeSmtpServer) -> FakeSmtpServer:
    """干净起步的捕获端；投递经生产的发信装配路径打到它上面（见 `integration_environment`）。"""
    smtp_server.reset()
    return smtp_server


# ---------------------------------------------------------------------------
# 三、进程内假审查与假 IMAP（`providers/` 接口的实现，不含业务判定）
# ---------------------------------------------------------------------------


class FakeReviewError(RuntimeError):
    """假审查「抛异常」档位抛出的具体异常类型（`design.md` §4.2 的出错路径）。"""


#: 假审查的四个档位（取值由本夹具定义，属测试基础设施、不是产品语义）。
type _ReviewMode = Literal["pass", "reject", "raise", "hang"]


class FakeReviewer:
    """可替换的假审查实现：只按**档位**回答，不做任何内容判定（`plan.md` §7.2）。

    四个档位：``pass``（恒放行，等价一期空实现 `NFR-6`）、``reject``（恒拒审，`AC-6` /
    `AC-12` 的反例档）、``raise``（抛 `FakeReviewError`，走 `source = review_error`）、
    ``hang``（永不返回，由调用方的 `REVIEW_TIMEOUT_SECONDS` 收场，`AC-40`）。

    `reviewed` 只是把收到的中性输入留痕，供用例断言**替换真的生效**；用例不应断言调用次数。
    """

    def __init__(self, mode: _ReviewMode = "pass") -> None:
        self.mode: _ReviewMode = mode
        self.reviewed: list[ReviewMessage] = []

    async def review(self, message: ReviewMessage) -> ReviewDecision:
        """按当前档位回答（哪个档位与内容无关）。"""
        self.reviewed.append(message)
        mode = self.mode
        if mode == "raise":
            raise FakeReviewError("假审查：按档位抛异常")
        if mode == "hang":
            await asyncio.Event().wait()
            return "pass"  # 不可达：hang 档由墙钟中断，仅为让返回类型完整
        return "pass" if mode == "pass" else "reject"


@pytest.fixture
def fake_review(monkeypatch: pytest.MonkeyPatch) -> FakeReviewer:
    """把 `providers/review/factory.py` 的工厂换成可脚本化的假实现（`plan.md` §7.2）。

    替换点是仓库里固定的那个（该模块的 docstring 已声明）：两条审查流都在**运行期**调工厂取
    审查器，所以 import 期绑定返回值的写法不会绕过本夹具。
    """
    fake = FakeReviewer()
    monkeypatch.setattr(review_factory, "create_reviewer", lambda: fake)
    return fake


class FakeImapFetcher:
    """进程内假 IMAP：按注入的中性样本给出 `UIDVALIDITY` / `UIDNEXT` 增量（`plan.md` §7.2）。

    只替代「外部 IMAP 服务」这一进程边界，交出的仍是 `providers/imap/base.py` 的中性结构：
    分拣判定（DSN / MDN / 引用匹配）一概不在这里——那是 `core/inbound.py` 的被测对象。

    游标回退不需要本对象配合：游标在库里，用例把 `imap_cursors.last_uid` 拨回去再跑一次 tick，
    同一封会被再取一次——真实 IMAP 就是这个行为（`AC-39`）。
    """

    def __init__(self, *, mailbox: str) -> None:
        self.mailbox = mailbox
        self.uidvalidity = 1
        self.uidnext = 1
        self.messages: list[FetchedMessage] = []
        self.fetch_requests: list[tuple[int, int]] = []
        self.error_class: ImapErrorClass | None = None

    def add_message(
        self,
        *,
        content_type: str = "text/plain",
        headers: Mapping[str, str] | None = None,
        parts: Sequence[FetchedPart] = (),
        uid: int | None = None,
    ) -> FetchedMessage:
        """往信箱里放一封样本；不指定 `uid` 时按 `UIDNEXT` 递增分配并推进 `UIDNEXT`。"""
        message = FetchedMessage(
            mailbox=self.mailbox,
            uidvalidity=self.uidvalidity,
            uid=self.uidnext if uid is None else uid,
            content_type=content_type,
            headers={} if headers is None else dict(headers),
            parts=tuple(parts),
        )
        self.messages.append(message)
        self.uidnext = max(self.uidnext, message.uid) + 1
        return message

    def rotate_uidvalidity(self, *, uidvalidity: int, uidnext: int = 1) -> None:
        """模拟 `UIDVALIDITY` 轮换：信箱内容整体作废、进度重置（`design.md` §3.4）。"""
        self.uidvalidity = uidvalidity
        self.uidnext = uidnext
        self.messages = []

    def fail_with(self, error_class: ImapErrorClass) -> None:
        """注入受控失败（连接 / TLS / 超时的替身），下一次读状态与取回都返回它。"""
        self.error_class = error_class

    def recover(self) -> None:
        """撤销失败注入，回到正常取回。"""
        self.error_class = None

    async def read_mailbox_state(self, *, timeout_seconds: int | None = None) -> MailboxStateResult:
        """`SELECT` 一次信箱：给出 `UIDVALIDITY` / `UIDNEXT`，或受控失败分类。"""
        if self.error_class is not None:
            return MailboxStateResult(error_class=self.error_class)
        return MailboxStateResult(
            state=MailboxState(
                mailbox=self.mailbox, uidvalidity=self.uidvalidity, uidnext=self.uidnext
            )
        )

    async def fetch_messages(
        self, *, after_uid: int, limit: int, timeout_seconds: int | None = None
    ) -> FetchResult:
        """取回 UID 严格大于 `after_uid` 的最多 `limit` 封、按 UID 升序（接口契约）。"""
        self.fetch_requests.append((after_uid, limit))
        if self.error_class is not None:
            return FetchResult(error_class=self.error_class)
        due = [
            message
            for message in sorted(self.messages, key=lambda item: item.uid)
            if message.uid > after_uid
        ]
        return FetchResult(messages=tuple(due[:limit]))


@pytest.fixture
def imap_fake(
    monkeypatch: pytest.MonkeyPatch, integration_environment: Settings
) -> FakeImapFetcher:
    """把 `flows/fetch_inbound.py` 的装配点换成进程内假 IMAP（`plan.md` §7.2）。

    装配函数在流模块的 docstring 里被点名为阶段 5 的替换处，且流在**运行期**调用它。
    """
    fake = FakeImapFetcher(mailbox=integration_environment.imap_mailbox)
    monkeypatch.setattr(fetch_inbound, "_build_fetcher", lambda _settings: fake)
    return fake


# ---------------------------------------------------------------------------
# 四、配置隔离、引擎、会话与清表
# ---------------------------------------------------------------------------


def _env_text(value: str | int | bool) -> str:
    """环境变量取值：布尔给 pydantic-settings 认的小写文本。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


@pytest.fixture(autouse=True)
def integration_environment(
    test_database: TestDatabase,
    smtp_server: FakeSmtpServer,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Settings]:
    """把配置指向本次运行的测试库与进程内替身（只写环境变量，不改仓库里的配置文件）。

    这些取值同时让「测试不依赖某台开发机的 `.env`」成立：数据库是隔离库、SMTP 是进程内替身、
    身份与信箱用 `example.com` 占位地址（`AGENTS.md` §8 第 4 条）。启动校验的三条判据
    （`design.md` §4.4）在这里也满足：`IMAP_ADDRESS` 与 `REPLY_TO_ADDRESS` 取同一个值。
    """
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", test_database.url)
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("SMTP_PORT", str(smtp_server.port))
    monkeypatch.setenv("SMTP_STARTTLS", "false")
    monkeypatch.setenv("SMTP_USERNAME", "")
    monkeypatch.setenv("SMTP_PASSWORD", "")
    monkeypatch.setenv("MAIL_FROM_ADDRESS", _PLACEHOLDER_FROM)
    monkeypatch.setenv("REPLY_TO_ADDRESS", _PLACEHOLDER_REPLY_TO)
    monkeypatch.setenv("IMAP_ADDRESS", _PLACEHOLDER_REPLY_TO)
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
async def engine(integration_environment: Settings) -> AsyncIterator[AsyncEngine]:
    """每个用例一套异步引擎，用例结束 `dispose()`（`plan.md` §7.2；`docs/architecture.md` §5）。"""
    engine = create_async_engine(integration_environment.database_url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """短会话工厂：绑定用例自己的引擎，`expire_on_commit=False`（与生产口径一致）。"""
    return async_sessionmaker(bind=engine, expire_on_commit=False)


#: 清表语句：表名取自 ORM 元数据（结构真源在 `db/models.py`），DDL 级操作、不是 mock。
_TRUNCATE_TABLES = text(
    "TRUNCATE TABLE "
    + ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    + " RESTART IDENTITY CASCADE"
)


async def _truncate_tables(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(_TRUNCATE_TABLES)


@pytest.fixture(autouse=True)
async def clean_tables(engine: AsyncEngine) -> AsyncIterator[None]:
    """每个用例前后清空隔离库的四张表（`docs/testing.md` §4.3：用例自建数据、结束清理）。"""
    await _truncate_tables(engine)
    try:
        yield
    finally:
        await _truncate_tables(engine)


def _drop_process_engine() -> None:
    """丢掉进程级单例引擎，且不在**新**事件循环里关**旧**循环的连接。

    为什么不用 `db.session.dispose_engine()`：它按 `close=True` 走，会拿当前（新）事件循环去关
    上一个用例循环里的 asyncpg 连接，跨循环关闭会抛 `RuntimeError: Event loop is closed`。
    `close=False` 只把旧连接池去引用、不碰旧连接（SQLAlchemy 的语义），这正是「用例之间不共享
    进程级状态」（`docs/testing.md` R12）要的效果。

    ⚠️ 耦合警示：本函数直读 `db/session.py` 的**私有槽位** `_engine`（消费侧的记录在案权衡）。
    该槽位一旦重命名、`dispose_engine()` 的 `close` 语义一变、或单例改由别的机制持有，**必须
    同步改这里**——否则夹具会静默失效：隔离性丢失但用例照样绿，缺陷只在并发或跨用例串扰时显形。
    根治（`db/session.py` 提供测试可用的 `dispose(close=False)` 公共入口）涉及 `src/`，属范围外，
    **另开任务卡**，不在夹具卡里动。
    """
    engine = db_session._engine
    if engine is None:
        return
    db_session._engine = None
    asyncio.run(engine.dispose(close=False))


@pytest.fixture(autouse=True)
def isolated_process_engine() -> Iterator[None]:
    """用例之间不共享 `db/session.py` 的进程级单例引擎（`docs/architecture.md` §5）。

    同步夹具：`service/` 与 `api/` 经单例引擎开短会话，而单例是进程级全局状态；每个用例前后
    各丢一次，连接池与事件循环都不会跨用例串。
    """
    _drop_process_engine()
    yield
    _drop_process_engine()


# ---------------------------------------------------------------------------
# 五、用例可用的其他夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def settings_override(
    integration_environment: Settings, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., Settings]:
    """按用例覆盖配置项（`POLL_*` / `BATCH_SIZE` / 上限一类），返回覆盖后的配置。

    覆盖只写环境变量再清缓存，不改仓库里的配置文件（`docs/testing.md` §4.3）。调用形如
    ``settings_override(BATCH_SIZE=2)``。
    """

    def apply(**values: str | int | bool) -> Settings:
        for name, value in values.items():
            monkeypatch.setenv(name.upper(), _env_text(value))
        get_settings.cache_clear()
        return get_settings()

    return apply


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """静态邮件样本目录（DSN / MDN / 回复 / 原邮件）：代码与数据分离（`plan.md` §7.2）。"""
    return _INTEGRATION_DIR.parent / "fixtures"


# ---------------------------------------------------------------------------
# 六、收集钩子：本目录用例一律打标记；依赖不可达时整组跳过
# ---------------------------------------------------------------------------


def _is_integration_item(item: pytest.Item) -> bool:
    """本条用例是否落在本目录下（按路径判定，不依赖用例自己是否打了标记）。"""
    path = getattr(item, "path", None)
    return path is not None and Path(path).resolve().is_relative_to(_INTEGRATION_DIR)


def _announce_group_skip(config: pytest.Config, reason: str) -> None:
    """把「整组跳过」的原因写到终端。

    `pytest -q` 默认只打印 `N skipped`、不打印跳过原因，而 `docs/testing.md` §8 要求
    「跳过的输出必须写明为什么跳过」且**不允许静默跳过**——本行就是那条输出（跳过标记本身
    也带同样的原因，`-rs` / `-v` 下会再列一次）。
    """
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(f"\n[集成测试整组跳过] {reason}", red=True)


def pytest_collection_modifyitems(config: pytest.Config, items: Sequence[pytest.Item]) -> None:
    """本目录的用例一律打 `integration` 标记；依赖不可达时**整组跳过**（`docs/testing.md` §8）。

    * 标记：本目录的用例按约定必须打 `@pytest.mark.integration`（§8），这里再兜一层，
      免得将来新文件漏打标记而逃过跳过机制。
    * 跳过而不是失败：`scripts/check.sh` 刻意不起容器，容器未起时门禁仍要能跑（§8）；
      但**跳过不等于通过**，涉及数据库语义的验收必须在起了容器的前提下跑完（§6 第 7 条）。
      跳过原因写明是哪一项依赖不可达，并把原因打到终端，不允许静默跳过。
    """
    local_items = [item for item in items if _is_integration_item(item)]
    if not local_items:
        return
    for item in local_items:
        item.add_marker(pytest.mark.integration)
    status = _dependency_status()
    if status.is_reachable:
        return
    skip_marker = pytest.mark.skip(reason=status.reason)
    for item in local_items:
        item.add_marker(skip_marker)
    _announce_group_skip(config, status.reason)


__all__ = [
    "CapturedDelivery",
    "FakeImapFetcher",
    "FakeReviewError",
    "FakeReviewer",
    "FakeSmtpServer",
    "TestDatabase",
]
