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

from mail_relay.main import create_app  # noqa: E402


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """提供挂载了全部路由的测试客户端；退出时触发 lifespan 关闭阶段。"""
    with TestClient(create_app()) as test_client:
        yield test_client
