#!/usr/bin/env python3
"""仓储层语句的静态检查（`AC-26`）：认领写法唯一、无裸 SQL。

验证的业务规则 / 不变量（真源：`specs/0001-email-forwarding-service/plan.md` §2.9、
`AGENTS.md` §8.1）：

* **抢占硬约束**：认领语句必须同时编译出 ``FOR UPDATE`` 与 ``SKIP LOCKED``，
  且认领谓词同时含 ``attempt_count <= 4`` 与 ``next_attempt_at <= <绑定的 now 参数>``；
* **禁止两步写法**：仓储模块里不得出现 ``text()`` 裸串或裸 SQL 字符串
  （那是绕过构造器口径的注入面），认领必须以 ``with_for_update(skip_locked=True)`` 锁行；
* **条件写入 / 幂等插入**：过审落库语句带 ``ON CONFLICT DO NOTHING``（撞键按幂等忽略）。

期望值（子串、谓词、上界 4）全部来自规格文本，不来自被测代码。
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from sqlalchemy.dialects import postgresql

from vs_email_forwarding.constants import MAX_CLAIMABLE_ATTEMPT_COUNT
from vs_email_forwarding.db.repositories import inbound, pending


def _repository_root() -> Path:
    """锚定真实仓库根：从本文件向上找到 ``.git`` 所在目录。

    不能用 ``parents[2]`` 固定回数推导：mutmut 把测试复制到 ``mutants/tests/``
    下运行，``parents[2]`` 会解析到 ``mutants/``，扫描对象变成 mutmut 自生成的
    变异体（含 ``skip_locked=None`` 之类变异函数），stats 采集必红——五步门禁
    第五步自 T-6 起结构性红（门禁发现 GATE-T16-MUTMUT-1）。``mutants/`` 下
    没有 ``.git``（却有 pyproject.toml，故不能拿它当锚），向上找 ``.git``
    恒定落在真实仓库根。
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").exists():
            return parent
    raise AssertionError("测试不在仓库内运行：向上未找到 .git")


_REPOSITORIES_DIR: Final = (
    _repository_root() / "src" / "vs_email_forwarding" / "db" / "repositories"
)

#: 规格规定的认领谓词上界（attempt_count <= 4）。与 constants 断言一致，防漂移。
_EXPECTED_CLAIM_ATTEMPT_BOUND: Final = 4

#: 仓储层应有的模块清单（plan.md §2.4 的六个仓储模块 + 包 ``__init__``）。
#: 扫描面守卫的期望值来自规格，不来自被测目录的运行时内容。
_EXPECTED_REPOSITORY_MODULES: Final = frozenset(
    {
        "__init__.py",
        "cleanup.py",
        "cursors.py",
        "inbound.py",
        "messages.py",
        "pending.py",
        "rejected.py",
    }
)


def _repository_sources() -> dict[str, str]:
    """读出真实仓储层全部源码（``.git`` 锚定，mutmut 环境下不指向 ``mutants/``）。"""
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(_REPOSITORIES_DIR.rglob("*.py"))
    }


def _drop_mutmut_functions(tree: ast.Module) -> ast.Module:
    """就地剔除 mutmut 生成的 ``x_*__mutmut_*`` 变异函数（第二道防线）。

    mutmut 3 的 trampoline 会把每个函数复制为 ``x_<名>__mutmut_orig`` 与
    ``x_<名>__mutmut_<n>``（各变异版）；即使扫描面意外落进 ``mutants/``，
    被审对象也只是真实函数，不是变异体。
    """
    tree.body = [
        node
        for node in tree.body
        if not (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and "__mutmut_" in node.name
        )
    ]
    return tree


def _repository_trees() -> dict[str, ast.Module]:
    """全部仓储模块的语法树（已剔除变异函数）：三条 AST 扫描共用本口径。"""
    return {
        name: _drop_mutmut_functions(ast.parse(source, filename=name))
        for name, source in _repository_sources().items()
    }


class TestClaimStatementCompilesWithSkipLocked:
    """认领语句的编译产物必须同时含 FOR UPDATE 与 SKIP LOCKED（AC-26）。"""

    def test_claim_select_compiles_with_for_update_skip_locked(self) -> None:
        now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        statement = pending.claim_due_statement(direction="outbound", now=now, batch_size=10)

        sql = str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )

        # 期望值来自规格文本：认领 = FOR UPDATE + SKIP LOCKED（AGENTS.md §8.1 / plan §2.9）。
        assert "FOR UPDATE" in sql
        assert "SKIP LOCKED" in sql

    def test_claim_predicate_contains_attempt_bound_and_bound_now(self) -> None:
        now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        statement = pending.claim_due_statement(direction="outbound", now=now, batch_size=10)

        sql = str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )

        # 认领谓词（plan §5.2）：attempt_count <= 4 且 next_attempt_at <= 绑定的 now。
        assert f"attempt_count <= {_EXPECTED_CLAIM_ATTEMPT_BOUND}" in sql
        assert "next_attempt_at <=" in sql
        assert "2025-01-01 12:00:00" in sql  # now 是绑定参数，不是 func.now()

    def test_claim_attempt_bound_matches_constants(self) -> None:
        # 上界只有一个出处（constants），仓储不得另写数字。
        assert _EXPECTED_CLAIM_ATTEMPT_BOUND == MAX_CLAIMABLE_ATTEMPT_COUNT


class TestApprovalStatementIdempotent:
    """过审落库语句：INSERT INTO messages + ON CONFLICT (id) DO NOTHING（撞键按幂等忽略）。"""

    def test_approval_insert_compiles_with_on_conflict_do_nothing(self) -> None:
        statement = pending.approval_insert_statement()

        sql = str(statement.compile(dialect=postgresql.dialect()))

        assert "INSERT INTO messages" in sql
        # plan §2.9 的已验证写法：冲突目标显式为主键 id。
        assert "ON CONFLICT (id) DO NOTHING" in sql

    def test_approval_insert_resets_attempt_columns(self) -> None:
        # 跨表搬运建行即重置：attempt_count、next_attempt_at 必须在写入列里（design §2.4）。
        statement = pending.approval_insert_statement()

        sql = str(statement.compile(dialect=postgresql.dialect()))

        assert "attempt_count" in sql
        assert "next_attempt_at" in sql


class TestNoBareSqlOrTwoStepClaim:
    """AST 扫描：仓储层无 text() 裸串；认领写法唯一（AC-26）。

    三条扫描与扫描面守卫共用 :func:`_repository_trees` 的同一口径：
    ``.git`` 锚定的真实 src + 剔除 ``x_*__mutmut_*`` 变异函数（双防线）。
    """

    def test_scan_surface_is_real_repository_source(self) -> None:
        # 扫描面守卫：断言确实作用于真实 src——模块清单齐、非空、路径不含
        # mutants/（mutmut 把本文件复制到 mutants/tests/ 下运行，锚定失效时
        # 这条最先红，不会让别的断言拿变异体当被审对象）。
        assert "mutants" not in str(_REPOSITORIES_DIR)
        assert set(_repository_sources()) == _EXPECTED_REPOSITORY_MODULES

    def test_no_text_calls_in_repositories(self) -> None:
        for name, tree in _repository_trees().items():
            for node in ast.walk(tree):
                assert not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, (ast.Name, ast.Attribute))
                    and getattr(node.func, "id", getattr(node.func, "attr", None)) == "text"
                ), f"{name}: 仓储层禁止 text() 裸串"

    def test_no_raw_sql_string_literals_in_repositories(self) -> None:
        forbidden = ("SELECT ", "INSERT INTO", "UPDATE ", "DELETE FROM")
        for name, tree in _repository_trees().items():
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    for fragment in forbidden:
                        assert not node.value.upper().startswith(fragment), (
                            f"{name}: 出现疑似裸 SQL 字符串 {node.value!r}"
                        )

    def test_claim_builder_uses_with_for_update_skip_locked(self) -> None:
        # 「两步写法」的防线：认领必须以 with_for_update(skip_locked=True) 锁行取数，
        # 而不是先取数判断再更新。
        tree = _repository_trees()["pending.py"]
        found = False
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "with_for_update"
            ):
                keywords = {kw.arg: kw.value for kw in node.keywords}
                skip_locked = keywords.get("skip_locked")
                assert isinstance(skip_locked, ast.Constant) and skip_locked.value is True, (
                    "认领必须 skip_locked=True"
                )
                found = True
        assert found, "认领语句构造缺少 with_for_update 调用"

    def test_claim_batch_size_is_bound_parameter_not_magic_number(self) -> None:
        # 每批条数必须是入参（AGENTS.md §8.2：批次大小不得写死在代码里）。
        signature = inspect.signature(pending.claim_due_statement)
        assert "batch_size" in signature.parameters

    def test_backoff_table_is_referenced_not_duplicated(self) -> None:
        # 退避算术的真源是 constants 的退避表（15s / 30s / 60s，AC-9 的输入）；
        # 这里只断言仓储引用它，不自行携带第二份数字。
        assert "RETRY_BACKOFF_SECONDS" in _repository_sources()["pending.py"]


class TestInboundIdempotentInsert:
    """入库幂等插入（`AC-11`）：两处入站唯一键的冲突目标必须显式声明（plan §2.9）。

    冲突目标是 ``(mailbox, uidvalidity, uid)`` **部分唯一索引**（``WHERE direction =
    'inbound'``，design.md §2.2）——推断部分索引必须带同一 WHERE 谓词，否则 PostgreSQL
    找不到匹配的唯一约束；裸 ``on_conflict_do_nothing()`` 也因此被打回（须带显式目标）。
    """

    def test_inbound_pending_insert_targets_partial_unique_key(self) -> None:
        sql = str(
            inbound.inbound_pending_insert_statement().compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )

        assert "INSERT INTO pending_messages" in sql
        # 冲突目标 = 三列入站唯一键 + 部分索引推断谓词（design.md §2.2 索引表）。
        assert (
            "ON CONFLICT (mailbox, uidvalidity, uid) WHERE direction = 'inbound' DO NOTHING" in sql
        )

    def test_inbound_received_insert_targets_partial_unique_key(self) -> None:
        sql = str(
            inbound.inbound_received_insert_statement().compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )

        assert "INSERT INTO messages" in sql
        # messages 侧同键同谓词：过审删行后游标回退重拉的重复由它兜底（design.md §2.2）。
        assert (
            "ON CONFLICT (mailbox, uidvalidity, uid) WHERE direction = 'inbound' DO NOTHING" in sql
        )
