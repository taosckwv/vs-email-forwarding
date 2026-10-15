#!/usr/bin/env python3
"""配置口径的静态检查（spec 0001 / T-32 / `AC-27`）。

`AC-27` 要求两条：

1. `src/` 里**只有** `config.py` 读环境变量或读 `.env`——其它模块一律经
   `vs_email_forwarding.config.get_settings()`（`AGENTS.md` §7「配置只从 config 读」、`NFR-4`）；
2. `git ls-files` 含 `.env.example`、**不含** `.env`（`AGENTS.md` §8 第 6 条）。

期望值出处：`AGENTS.md` §7 / §8 第 6 条与 `AC-27` 自己给定的清单。扫描器另有「自己会咬」的
用例：喂进合成的违规源码、断言逐行行号——避免出现一个永远返回空列表的恒绿检查
（`docs/testing.md` 的 R2 / R5 取向：期望值独立推导、检查必须能被判假）。
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from typing import Final

#: `os` 上与读 / 写环境变量有关的属性名——写环境变量同样绕过了唯一配置入口。
_OS_ENV_ATTRS: Final = ("environ", "getenv", "putenv", "setenv")

#: 唯一允许读 `.env` 的模块（相对 `src/vs_email_forwarding/` 的 POSIX 路径）。
_CONFIG_MODULE: Final = "config.py"


def _repository_root() -> Path:
    """锚定真实仓库根：从本文件向上找到 `.git` 所在目录。

    不能按固定层数回推：mutmut 把工程复制到 `mutants/` 下运行，固定层数会落到 `mutants/`，
    扫描对象就变成变异体。口径与理由同 `tests/unit/test_repository_statements.py`。
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").exists():
            return parent
    raise AssertionError("测试不在仓库内运行：向上未找到 .git")


_SRC_DIR: Final = _repository_root() / "src" / "vs_email_forwarding"


def _sources() -> dict[str, str]:
    """`src/vs_email_forwarding/` 下的全部 `.py` 源码，键是相对该目录的 POSIX 路径。"""
    return {
        path.relative_to(_SRC_DIR).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(_SRC_DIR.rglob("*.py"))
    }


def _reads_environment(node: ast.AST, os_aliases: set[str]) -> bool:
    """该 AST 节点是否在直接访问环境变量（三种形态：导入、名字、`os` 属性）。"""
    if isinstance(node, ast.ImportFrom):
        return node.module == "os" and any(alias.name in _OS_ENV_ATTRS for alias in node.names)
    if isinstance(node, ast.Name):
        return node.id in _OS_ENV_ATTRS
    return (
        isinstance(node, ast.Attribute)
        and node.attr in _OS_ENV_ATTRS
        and isinstance(node.value, ast.Name)
        and node.value.id in os_aliases
    )


def _os_env_lines(source: str, *, filename: str) -> list[int]:
    """列出访问环境变量的位置，含 `os.environ` / `os.getenv` 与 `from os import environ`。"""
    tree = ast.parse(source, filename=filename)
    os_aliases = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "os"
    }
    return sorted(node.lineno for node in ast.walk(tree) if _reads_environment(node, os_aliases))


def _env_file_lines(source: str, *, filename: str) -> list[int]:
    """列出构造 pydantic-settings 点文件来源的位置（`SettingsConfigDict(env_file=...)`）。

    `env_file=None` 明确关掉点文件读取、不构成「第二处读 `.env`」，因此不算违规
    （`tests/unit/test_scheduler_intervals.py` 就用它构造只认显式取值的配置对象）。
    """
    hits: list[int] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not isinstance(node, ast.keyword) or node.arg != "env_file":
            continue
        if isinstance(node.value, ast.Constant) and node.value.value is None:
            continue
        hits.append(node.lineno)
    return sorted(hits)


def _tracked_paths() -> set[str]:
    """`git ls-files` 的仓库内路径集合（`AC-27` 指定的判定手段）。"""
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=_repository_root(),
        capture_output=True,
        text=True,
        check=True,
    )
    return set(result.stdout.splitlines())


def test_scan_face_is_not_empty() -> None:
    """扫描面必须真读到源码：源文件没读到时，「没有违规」是空承诺。"""
    sources = _sources()
    assert _CONFIG_MODULE in sources
    assert len(sources) >= 10


def test_only_config_module_reads_environment_variables() -> None:
    """除 `config.py` 外，`src/` 里没有模块读 / 写环境变量（`AGENTS.md` §7）。"""
    offenders = {
        name: lines
        for name, source in _sources().items()
        if name != _CONFIG_MODULE and (lines := _os_env_lines(source, filename=name))
    }
    assert offenders == {}


def test_only_config_module_reads_dotenv() -> None:
    """除 `config.py` 外，`src/` 里没有第二处配置点文件读取；`config.py` 自己确实在读。"""
    offenders = {
        name: lines
        for name, source in _sources().items()
        if name != _CONFIG_MODULE and (lines := _env_file_lines(source, filename=name))
    }
    assert offenders == {}

    config_source = _sources()[_CONFIG_MODULE]
    assert _env_file_lines(config_source, filename=_CONFIG_MODULE)
    assert 'env_file=".env"' in config_source


def test_scanner_flags_synthetic_violations() -> None:
    """扫描器自己会咬：合成源码的违规行号 == 手写期望（反恒绿）。"""
    synthetic_env_reads = (
        "import os\n"
        "import os as _os\n"
        "from os import environ\n"
        "first = os.environ['A']\n"
        "second = _os.getenv('B')\n"
        "third = environ['C']\n"
    )
    assert _os_env_lines(synthetic_env_reads, filename="synthetic.py") == [3, 4, 5, 6]

    synthetic_env_file = (
        "from pydantic_settings import SettingsConfigDict\n"
        'SettingsConfigDict(env_file=".env")\n'
        "SettingsConfigDict(env_file=None)\n"
    )
    assert _env_file_lines(synthetic_env_file, filename="synthetic.py") == [2]


def test_env_files_are_tracked_correctly() -> None:
    """`git ls-files` 含 `.env.example`、不含 `.env`（`AGENTS.md` §8 第 6 条）。"""
    tracked = _tracked_paths()
    assert ".env.example" in tracked
    assert [path for path in tracked if path == ".env" or path.endswith("/.env")] == []
