#!/usr/bin/env python3
"""集成夹具的常驻契约测试（spec 0001 / `T-33` / `T-33b`）。

守护对象是 `tests/integration/conftest.py` 的夹具与三条进程内替身。T-33 的五条验收点各有落点
（括号里是守护它的用例，改夹具时先看这里）：真实 PostgreSQL 隔离库（`TestDatabaseIsolation`）、
容器未起时整组跳过（`TestCollectionHookGroupSkip`：直接调用收集钩子，在「可达 / 不可达」两个分支
上判定）、替身经 `providers/` 接口注入（`TestFakeReview` / `TestFakeImap` / `TestSmtpCapture` 与
`TestDoubleContractConsistency`）、用例间清表（`TestTableCleanupBetweenTests`）、线上字节捕获
（`TestCapturedDeliveryParsing`）。替身与**真实现**还要过同一套接口契约（`docs/testing.md` §4.2：
「同一个 interface 的契约测试，真实现与替身都要过」）。

期望值来源（`docs/testing.md` R2）全部独立于 `conftest.py` 的实现：

* 四张业务表的名字来自 `design.md` §2.1（不是从 `Base.metadata` 反推）；
* `From` / `Reply-To` 的期望值取自 `config.get_settings()`（配置真源），断言对象是**线上字节**；
* 库名形状取自本目录 `conftest.py` 模块 docstring 的七条口径 #2（经 T-33 评审确认）；`4xx` / `5xx`
  区间取自 `plan.md` §7.2 / §8.1 与 `constants.py` 的区间口径；
* 替身契约的方法名、参数表取自 `providers/*/base.py` 的 `Protocol`（用 `inspect` 读契约本身，
  不是抄一份签名到测试里）；
* 「假审查只按档位回答」由「同一输入在两个档位下结果不同」反证——输出只由档位决定。

夹具对象一律经 pytest 参数注入取得，本文件不手工 import 任何夹具；唯一一处 import 是替身自定义的
`FakeReviewError`（只有本目录 `conftest.py` 定义它，没有别处可取），模块名撞车的风险因此由这一处
承担。夹具参数的注解一律用 `Any`，类型信息以 conftest 里的类定义为准（`mypy` 的范围是 `src/`，
不检查测试）。
"""

from __future__ import annotations

import email
import inspect
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from conftest import FakeReviewError
from vs_email_forwarding.config import get_settings
from vs_email_forwarding.db import session as db_session
from vs_email_forwarding.db.models import Message, PendingMessage
from vs_email_forwarding.flows import fetch_inbound, review_outbound, send_email
from vs_email_forwarding.providers.imap.base import FetchedPart, ImapFetcher
from vs_email_forwarding.providers.imap.imapclient_fetcher import ImapClientFetcher, ImapConfig
from vs_email_forwarding.providers.review.always_pass import AlwaysPassReviewer
from vs_email_forwarding.providers.review.base import Reviewer, ReviewMessage
from vs_email_forwarding.providers.smtp.base import OutboundMessage

pytestmark = pytest.mark.integration

_NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)


def _review_message(direction: str = "outbound") -> ReviewMessage:
    """一条中性待审输入（夹具数据；与假审查的档位无关）。"""
    return ReviewMessage(
        direction=direction,  # type: ignore[arg-type]
        message_id=uuid.uuid4(),
        recipient="colleague@example.com",
        subject="夹具主题",
        body="夹具正文",
        body_format="text",
    )


def _pending_row(*, pending_id: uuid.UUID) -> dict[str, Any]:
    """一条出站待审行的夹具数据（列取值来自 `design.md` §2.1 的字段表）。"""
    return {
        "id": pending_id,
        "direction": "outbound",
        "message_id": "<fixture@example.com>",
        "recipient": "colleague@example.com",
        "subject": "夹具主题（无真实收件人）",
        "body": "夹具正文",
        "attempt_count": 0,
        "next_attempt_at": _NOW,
        "created_at": _NOW,
    }


class TestDatabaseIsolation:
    """真实 PostgreSQL + 运行级唯一测试库（`plan.md` §7.2、§8.1 的库隔离兜底）。"""

    async def test_uses_real_postgresql_not_mock(self, engine: AsyncEngine) -> None:
        async with engine.connect() as conn:
            version = (await conn.execute(sa.text("select version()"))).scalar_one()
        assert "PostgreSQL" in version

    async def test_database_name_carries_run_scoped_suffix(self, test_database: Any) -> None:
        name = test_database.name
        assert re.fullmatch(r"vs_email_forwarding_test_\d+_[0-9a-f]{8}", name), name
        # 隔离真的生效：本次运行的库名不是覆盖前配置里那个库（本地 .env 的开发库）。
        assert name != test_database.configured_database

    async def test_schema_built_by_migrations(self, engine: AsyncEngine) -> None:
        async with engine.connect() as conn:
            tables = set(
                (
                    await conn.execute(
                        sa.text("select tablename from pg_tables where schemaname = 'public'")
                    )
                )
                .scalars()
                .all()
            )
        # 四张业务表的名字取自 design.md §2.1；迁移版本表证明建表走的是迁移而非 create_all。
        assert {"messages", "pending_messages", "rejected_messages", "imap_cursors"} <= tables
        assert "alembic_version" in tables


class TestTableCleanupBetweenTests:
    """用例间清表：上一个用例留下的行不会出现在下一个用例的起点（`docs/testing.md` §4.3）。

    成对设计（`a` 造脏、`b` 见空）：只有按 pytest 的文件内定义序执行才有守护意义，乱序或单独
    执行时 `b` 例是空转——它断言的「空」不是清表造成的。
    """

    async def test_a_leaves_dirty_rows(self, engine: AsyncEngine) -> None:
        async with engine.begin() as conn:
            for _ in range(2):
                await conn.execute(
                    sa.insert(PendingMessage).values(**_pending_row(pending_id=uuid.uuid4()))
                )
        async with engine.connect() as conn:
            count = len((await conn.execute(sa.select(PendingMessage.id))).all())
        assert count == 2

    async def test_b_starts_from_empty_tables(self, engine: AsyncEngine) -> None:
        async with engine.connect() as conn:
            count = len((await conn.execute(sa.select(PendingMessage.id))).all())
        assert count == 0


class TestFakeReview:
    """假审查：进程内、经 `providers/review/factory.py` 替换、只按档位回答。"""

    async def test_replacement_reaches_the_flow_lookup(self, fake_review: Any) -> None:
        reviewer = review_outbound.review_factory.create_reviewer()
        assert reviewer is fake_review
        assert not isinstance(reviewer, AlwaysPassReviewer)
        message = _review_message()
        assert await reviewer.review(message) == "pass"
        assert fake_review.reviewed == [message]

    async def test_decision_follows_the_scripted_mode_only(self, fake_review: Any) -> None:
        message = _review_message(direction="inbound")
        reviewer = review_outbound.review_factory.create_reviewer()
        passed = await reviewer.review(message)
        fake_review.mode = "reject"
        rejected = await reviewer.review(message)
        # 同一输入、同一实现，结果只由档位决定——假实现不含任何内容判定。
        assert passed == "pass"
        assert rejected == "reject"

    async def test_error_mode_raises(self, fake_review: Any) -> None:
        fake_review.mode = "raise"
        # 异常类型直接取自夹具模块声明的那个具体类，不再经夹具实例的属性间接取（断言意图不变：
        # raise 档必须抛该类型）。
        with pytest.raises(FakeReviewError):
            await review_outbound.review_factory.create_reviewer().review(_review_message())


async def _send_once(smtp_capture: Any) -> Any:
    """经**生产装配路径**取发信适配器并投一封——只有 SMTP 服务器是进程内替身。"""
    settings = get_settings()
    sender = send_email._build_sender(settings)
    return await sender.send_message(
        OutboundMessage(
            recipient="colleague@example.com",
            subject="夹具主题",
            body="夹具正文",
            body_format="text",
        ),
        timeout_seconds=settings.smtp_timeout_seconds,
    )


class TestSmtpCapture:
    """进程内假 SMTP：走真实 SMTP 协议与真实适配器，捕获端拿到线上字节。"""

    async def test_delivery_is_captured_from_the_wire(self, smtp_capture: Any) -> None:
        settings = get_settings()
        result = await _send_once(smtp_capture)
        assert result.accepted is True
        assert smtp_capture.delivery_count == 1
        delivery = smtp_capture.deliveries[0]
        # 期望值取自配置真源：From 只能是 MAIL_FROM_ADDRESS，Reply-To 只能是 REPLY_TO_ADDRESS。
        assert delivery.mail_from == settings.mail_from_address
        assert delivery.recipients == ("colleague@example.com",)
        assert delivery.header("From") == settings.mail_from_address
        assert delivery.header("Reply-To") == settings.reply_to_address
        assert delivery.header("To") == "colleague@example.com"

    async def test_transient_mode_is_a_4xx(self, smtp_capture: Any) -> None:
        smtp_capture.mode = "transient"
        result = await _send_once(smtp_capture)
        assert result.accepted is False
        assert result.error_class == "transient"
        assert result.code is not None
        assert 400 <= result.code <= 499  # 4xx 区间（constants 的 transient 区间）

    async def test_permanent_mode_is_a_5xx(self, smtp_capture: Any) -> None:
        smtp_capture.mode = "permanent"
        result = await _send_once(smtp_capture)
        assert result.accepted is False
        assert result.error_class == "permanent"
        assert result.code is not None
        assert 500 <= result.code <= 599

    async def test_leading_dot_line_survives_the_wire(self, smtp_capture: Any) -> None:
        """`RFC 5321` §4.5.2 的透明度：正文里以 `.` 开头的行，到捕获端仍是那一个点。

        期望值按 RFC 手写（不取自夹具实现）：发送方必须把 `'.hidden line'` 填充成
        `'..hidden line'`，接收方的去填充只去掉**那一个**前导点，于是实收报文里是
        `b"\\r\\n.hidden line\\r\\n"`；普通行不参与填充，必须逐字不变。
        """
        settings = get_settings()
        sender = send_email._build_sender(settings)
        body = "first line\n.hidden line\nlast line"
        result = await sender.send_message(
            OutboundMessage(
                recipient="colleague@example.com",
                subject="夹具主题",
                body=body,
                body_format="text",
            ),
            timeout_seconds=settings.smtp_timeout_seconds,
        )
        assert result.accepted is True
        delivery = smtp_capture.deliveries[0]
        # 去填充到位：正文行只带那一个点（填充残留会是两个点）。
        assert b"\r\n.hidden line\r\n" in delivery.raw
        assert b"..hidden line" not in delivery.raw
        # 普通行不变：既不被填充，也不被去填充误伤。
        assert b"first line\r\n" in delivery.raw
        assert b"last line" in delivery.raw


class TestFakeImap:
    """进程内假 IMAP：注入样本、按 UID 增量给封、支持游标回退重取。"""

    async def test_replacement_reaches_the_flow_lookup(self, imap_fake: Any) -> None:
        assert fetch_inbound._build_fetcher(get_settings()) is imap_fake

    async def test_serves_injected_samples_in_uid_order(self, imap_fake: Any) -> None:
        second = imap_fake.add_message(
            content_type="text/plain",
            headers={"in-reply-to": "<second@example.com>"},
            parts=(FetchedPart(content_type="text/plain", payload="第二封"),),
        )
        first = imap_fake.add_message(
            content_type="text/plain",
            headers={"in-reply-to": "<first@example.com>"},
            parts=(FetchedPart(content_type="text/plain", payload="第一封"),),
        )
        state = await imap_fake.read_mailbox_state()
        assert state.state is not None
        assert state.state.uidvalidity == imap_fake.uidvalidity
        assert state.state.uidnext == max(second.uid, first.uid) + 1

        result = await imap_fake.fetch_messages(after_uid=0, limit=10)
        assert result.error_class is None
        assert [message.uid for message in result.messages] == sorted([second.uid, first.uid])

        # 游标回退：把 after_uid 拨回去，同一封会被再取一次（AC-39 的库侧回滚场景）。
        again = await imap_fake.fetch_messages(after_uid=0, limit=10)
        assert again.messages == result.messages

    async def test_failure_is_a_controlled_result(self, imap_fake: Any) -> None:
        imap_fake.fail_with("transient")
        state = await imap_fake.read_mailbox_state()
        assert state.state is None
        assert state.error_class == "transient"


class TestAncillaryFixtures:
    """会话工厂与样本目录：T-34 ~ T-38 的共同地基。"""

    def test_session_factory_is_bound_to_the_test_engine(
        self, engine: AsyncEngine, session_factory: async_sessionmaker[Any]
    ) -> None:
        # 绑定到本用例的引擎（不是进程级单例），且与生产口径一致：提交后不过期。
        assert session_factory.kw["bind"] is engine
        assert session_factory.kw["expire_on_commit"] is False

    def test_fixtures_dir_points_at_static_samples(self, fixtures_dir: Path) -> None:
        assert fixtures_dir == Path(__file__).resolve().parents[1] / "fixtures"
        # 该目录此刻只有职责 README（AGENTS.md §4：非包目录用 README 占位，Git 才留得住它）；
        # 静态邮件样本（DSN / MDN / 回复 / 原邮件）后续落进来，路径契约不变。
        assert (fixtures_dir / "README.md").is_file()


class TestProcessEngineIsolation:
    """进程级单例引擎不跨用例：`service/` / `api/` 走的就是它（`docs/architecture.md` §5）。

    成对设计（`a` 起单例、`b` 断言单例已被丢弃）：只有按 pytest 的文件内定义序执行才有守护意义，
    乱序或单独执行时 `b` 例是空转。这里断言的 `_engine` 槽位就是**夹具契约本身**——与本目录
    `conftest.py` 的同名口径互为镜像，不是对被测实现内部结构的额外要求。
    """

    async def test_a_uses_the_process_level_singleton(self) -> None:
        factory = db_session.session_factory()
        async with factory() as session:
            value = (await session.execute(sa.text("select 1"))).scalar_one()
        assert value == 1
        assert db_session._engine is not None

    async def test_b_does_not_inherit_a_stale_singleton(self) -> None:
        # 夹具在每个用例前后丢掉单例引擎；上一个用例的连接池（与它的事件循环）不会串过来。
        assert db_session._engine is None
        factory = db_session.session_factory()
        async with factory() as session:
            assert (await session.execute(sa.text("select 1"))).scalar_one() == 1


class TestFixtureFoundationSmoke:
    """夹具地基：真实库 + 假审查 + 清表 能在一次真实 tick 上合起来跑通（T-34 的前置）。"""

    async def test_review_outbound_tick_runs_on_the_isolated_database(
        self, session_factory: async_sessionmaker[Any], fake_review: Any
    ) -> None:
        settings = get_settings()
        pending_id = uuid.uuid4()
        async with session_factory() as session:
            await session.execute(
                sa.insert(PendingMessage).values(**_pending_row(pending_id=pending_id))
            )
            await session.commit()

        result = await review_outbound.review_outbound_tick(session_factory, settings, now=_NOW)

        assert result.approved == 1
        async with session_factory() as session:
            messages = (await session.execute(sa.select(Message.id, Message.status))).all()
            pending = (await session.execute(sa.select(PendingMessage.id))).all()
        # 过审 → 待审行删除、目标行进 messages 且状态是「投递中」（design.md §3.2 流转）。
        assert messages == [(pending_id, "sending")]
        assert pending == []
        assert len(fake_review.reviewed) == 1


# ---------------------------------------------------------------------------
# 收集钩子：依赖不可达时整组跳过（T-33 验收点 ② 的常驻守护）
# ---------------------------------------------------------------------------


class _ItemProbe:
    """收集钩子的**条目替身**：只记录「谁被加了什么标记」，不触碰真实的 `pytest.Item`。

    钩子对条目只用两样东西——按 `.path` 认路、用 `add_marker()` 打标记（见
    `conftest._is_integration_item`），因此替身只需要这两样。用它断言钩子的**行为**，本用例
    自己就不会把整组真实用例标成跳过。
    """

    def __init__(self, item: pytest.Item) -> None:
        self.path = item.path
        self.markers: list[pytest.Mark] = []

    def add_marker(self, marker: pytest.Mark) -> None:
        self.markers.append(marker)

    @property
    def skip_reasons(self) -> list[str]:
        """本条目收到的 `skip` 标记的全部原因（按打标记的顺序）。"""
        return [
            str(marker.kwargs.get("reason", "")) for marker in self.markers if marker.name == "skip"
        ]


def _integration_conftest_module(config: pytest.Config) -> Any:
    """本目录 `conftest.py` 被 pytest 加载后的那个模块对象（按文件路径认，不按模块名）。

    两个 `conftest.py`（`tests/` 与 `tests/integration/`）的模块名都是 `conftest`，按名字取会撞车；
    钩子与依赖探测都住在本目录这一个模块里，这里按 `__file__` 精确定位 pytest 真正加载的实例，
    因此 `monkeypatch` 的结果就是钩子看到的结果。
    """
    expected = (Path(__file__).resolve().parent / "conftest.py").resolve()
    for plugin in config.pluginmanager.get_plugins():
        plugin_file = getattr(plugin, "__file__", None)
        if plugin_file is not None and Path(plugin_file).resolve() == expected:
            return plugin
    raise AssertionError(f"本目录的 conftest 未被 pytest 注册为插件：{expected}")


def _local_item_probes(request: pytest.FixtureRequest) -> list[_ItemProbe]:
    """本目录全部「已收集」条目的替身（按路径判定，不看条目自己有没有打标记）。"""
    integration_dir = Path(__file__).resolve().parent
    items = [
        item for item in request.session.items if integration_dir in item.path.resolve().parents
    ]
    assert items, "本目录没有任何条目被收集 ⇒ 守护对象为空"
    # 本文件自己也在其中：钩子必须对「包括这个文件在内」的全部条目生效。
    assert any(item.path.resolve() == Path(__file__).resolve() for item in items)
    return [_ItemProbe(item) for item in items]


class TestCollectionHookGroupSkip:
    """容器未起时整组跳过：`docs/testing.md` §6 第 7 条与 §8 的常驻守护（T-33 验收点 ②）。

    被守护的是本目录 `conftest.py::pytest_collection_modifyitems`，它对本目录**全部**条目做两件事：
    补 `integration` 标记；依赖不可达时给每条打上**带原因的** `skip` 标记，并把原因播报到终端
    （不允许静默跳过）。两个分支都在这里判定：

    * **可达**——本用例能跑到这里本身就是「收集时依赖可达」的证据（不可达时它会被同一条钩子标成
      跳过），因此此时不得出现任何 `skip` 标记；
    * **不可达**——把依赖探测换成注入的不可达结果后，本目录全部条目都要拿到非空原因，原因与播报
      内容一致，且传入的条目列表未被增删。

    少了第二个分支，这条守护只在一个方向上成立：「不可达就跳过」写错了（例如干脆不打标记）不会
    有任何用例变色。条目用替身包裹，两个分支都不会把真实用例标成跳过。
    """

    def test_reachable_dependencies_add_no_skip_marker(
        self, request: pytest.FixtureRequest
    ) -> None:
        module = _integration_conftest_module(request.config)
        status = module._dependency_status()
        assert status.is_reachable, f"依赖在本用例执行期间掉线：{status.reason}"
        probes = _local_item_probes(request)

        module.pytest_collection_modifyitems(request.config, list(probes))

        assert [probe.skip_reasons for probe in probes] == [[] for _ in probes]
        # 钩子的另一半：本目录每个条目都被补上 integration 标记（新文件漏打标记也逃不掉）。
        assert all(
            any(marker.name == "integration" for marker in probe.markers) for probe in probes
        )

    def test_unreachable_dependencies_skip_the_whole_group(
        self, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module = _integration_conftest_module(request.config)
        reason = "PostgreSQL 127.0.0.1:1 连接失败——依赖探测已由本用例替换（注入值，非真实状态）"
        monkeypatch.setattr(
            module,
            "_dependency_status",
            lambda: module._DependencyStatus(is_reachable=False, reason=reason),
        )
        announced: list[str] = []
        monkeypatch.setattr(
            module, "_announce_group_skip", lambda config, text: announced.append(text)
        )
        probes = _local_item_probes(request)
        passed_in = list(probes)

        module.pytest_collection_modifyitems(request.config, passed_in)

        assert passed_in == probes  # 条目未被移除、未被重排
        assert [probe.skip_reasons for probe in probes] == [[reason] for _ in probes]
        assert announced == [reason]  # 原因被播报：不允许静默跳过


# ---------------------------------------------------------------------------
# 替身与真实现的接口契约（docs/testing.md §4.2：同一个 interface 的契约测试，两者都要过）
# ---------------------------------------------------------------------------


def _protocol_parameters(protocol: type) -> dict[str, dict[str, tuple[str, bool]]]:
    """协议声明的公开方法及其参数表（契约真源是 `providers/*/base.py` 的 `Protocol`）。

    参数表的每一项是「参数名 → (参数种类, 是否有默认值)」：名字、关键字限定与默认值三项都
    属于调用方看得见的契约——少一个默认值或把 keyword-only 改成位置参数，调用点就会静默改变行为。
    """
    declarations: dict[str, dict[str, tuple[str, bool]]] = {}
    for name, member in inspect.getmembers(protocol, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        signature = inspect.signature(member)
        declarations[name] = {
            parameter_name: (
                parameter.kind.name,
                parameter.default is not inspect.Parameter.empty,
            )
            for parameter_name, parameter in signature.parameters.items()
            if parameter_name != "self"
        }
    return declarations


def _is_async(member: object) -> bool:
    """该方法是否为协程函数（契约里的 `async def` 是硬约束：调用方直接 `await` 它）。"""
    return inspect.iscoroutinefunction(member)


def _contract_violations(implementation: object, protocol: type) -> dict[str, str]:
    """实现相对协议的不一致：方法缺失 / 不可调用 / 参数表不同 / 同步实现异步契约。

    返回 `{方法名: 原因}`，空字典表示完全符合。**真实现与替身共用这一个检查**——两者都不满足
    即调用点会在运行期炸（`AttributeError` / `TypeError` / 拿到未被 await 的协程）。
    """
    violations: dict[str, str] = {}
    for name, expected_parameters in _protocol_parameters(protocol).items():
        member = getattr(implementation, name, None)
        if member is None:
            violations[name] = "契约声明的方法在这里不存在"
            continue
        if not callable(member):
            violations[name] = "存在但不可调用"
            continue
        if _is_async(member) is not _is_async(getattr(protocol, name)):
            violations[name] = "协程性不同：契约是 async def"
            continue
        actual = inspect.signature(member)
        actual_parameters = {
            parameter_name: (parameter.kind.name, parameter.default is not inspect.Parameter.empty)
            for parameter_name, parameter in actual.parameters.items()
            if parameter_name != "self"
        }
        if actual_parameters != expected_parameters:
            violations[name] = f"参数表不同：{actual_parameters} != {expected_parameters}"
    return violations


class _ReviewerMissingMethod:
    """对照实现：`review` 整个缺失（证明契约检查不是恒绿）。"""


class _ReviewerWithWrongParameters:
    """对照实现：方法是 `async` 但参数表与契约不符（少收 `message`）。"""

    async def review(self) -> str:
        """故意与契约不一致（契约是 `review(message)`）。"""
        return "pass"


class TestDoubleContractConsistency:
    """替身与真实现过同一套接口契约（`docs/testing.md` §4.2）。"""

    def test_contract_check_reports_violations_and_accepts_conformity(self) -> None:
        # 两侧都要成立：不存在的实现被判违规，合规的真实现零违规——否则这个检查本身没意义。
        # 「方法集」钉死，而不是「至少包含」：检查的覆盖面与被检查的方法集是两件事——少了这两条，
        # 契约增删一个公开方法都不会有任何用例变色（`_contract_violations` 会安静地跟着契约走）。
        # 期望值来自 design.md：§4.2 把审查接口描述成单方法接口（传入待审邮件、答 pass / reject），
        # §3.4 把入站拉取描述成「先读信箱状态、再按 UID 区间取回」两步；方法名的字面拼写以
        # `providers/*/base.py` 的 `Protocol` 为准，两侧任一改动都会在这里变红。
        assert set(_protocol_parameters(Reviewer)) == {"review"}
        assert set(_protocol_parameters(ImapFetcher)) == {"read_mailbox_state", "fetch_messages"}
        assert set(_contract_violations(_ReviewerMissingMethod(), Reviewer)) == {"review"}
        assert set(_contract_violations(_ReviewerWithWrongParameters(), Reviewer)) == {"review"}
        assert _contract_violations(AlwaysPassReviewer(), Reviewer) == {}

    def test_review_double_matches_the_reviewer_contract(self, fake_review: Any) -> None:
        assert _contract_violations(fake_review, Reviewer) == {}
        assert _contract_violations(AlwaysPassReviewer(), Reviewer) == {}

    def test_imap_double_matches_the_imapfetcher_contract(self, imap_fake: Any) -> None:
        assert _contract_violations(imap_fake, ImapFetcher) == {}
        assert (
            _contract_violations(
                ImapClientFetcher(
                    ImapConfig(
                        host="imap.example.com",
                        port=993,
                        username="placeholder",
                        password="placeholder",
                        mailbox="INBOX",
                        starttls=False,
                        timeout_seconds=10,
                    )
                ),
                ImapFetcher,
            )
            == {}
        )


class TestCapturedDeliveryParsing:
    """捕获到的原始报文是**完整报文**：信封身份与头部身份一致（`AGENTS.md` §8 第 3 条）。

    这里刻意不用 `CapturedDelivery.header()`（那是夹具自己的解析路径），改用标准库
    `email.message_from_bytes` 重新解析一次线上字节：信封上的 `MAIL FROM` / `RCPT TO` 必须与
    报文头里的 `From` / `To` 指向同一身份，否则收件端看到的就是两套身份。
    """

    async def test_raw_message_is_complete_and_identities_match(self, smtp_capture: Any) -> None:
        settings = get_settings()
        await _send_once(smtp_capture)
        delivery = smtp_capture.deliveries[0]

        assert b"\r\n\r\n" in delivery.raw  # 头与体之间的空行：报文不是被截断的半截
        parsed = email.message_from_bytes(delivery.raw)
        assert parsed["From"] == settings.mail_from_address
        assert parsed["From"] == delivery.mail_from
        assert parsed["Reply-To"] == settings.reply_to_address
        assert parsed["To"] == delivery.recipients[0]
        assert parsed.get_content_type() == "text/plain"
