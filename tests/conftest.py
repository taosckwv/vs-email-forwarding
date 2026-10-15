#!/usr/bin/env python3
"""pytest 公共夹具与测试环境准备。

``tests/`` 下各目录按仓库约定不放 ``__init__.py``，靠本文件与各目录的 ``README.md``
说明测试分层。
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# 让 src 布局在尚未执行 uv sync（包未安装）时也能被导入：把仓库根下的 src 加入 sys.path。
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC_DIR = _PROJECT_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from vs_email_forwarding.main import create_app  # noqa: E402


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """提供挂载了全部路由的测试客户端（**不进入 lifespan**）。

    `T-32` 之后 lifespan 会建进程级引擎并起五条后台流：单测用 `with TestClient(...)` 进入它，
    等于让 `tests/unit/` 真连 PostgreSQL / IMAP，违背 `AGENTS.md` §4「tests/unit/ 不依赖数据库
    与网络」与 `docs/testing.md` R12。lifespan 自身的装配（启动校验 → 引擎 → 五任务 → 关闭收尾）
    由 `tests/unit/test_settings_validation.py` 用假协作者逐条覆盖。
    """
    yield TestClient(create_app())
