"""健康检查接口的单元测试（骨架阶段唯一能真实验证的行为）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from mail_relay import __version__


def test_healthz_returns_ok(client: TestClient) -> None:
    """GET /healthz 应返回 200，且 status 为 ok、version 与包版本一致。"""
    response = client.get("/healthz")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["version"] == __version__
