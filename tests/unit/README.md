# 单元测试

放什么：不依赖数据库与网络的纯逻辑测试。用 `fastapi.testclient.TestClient` 走进程内
HTTP 调用，不启动 uvicorn、不连 PostgreSQL、不发真实邮件。

不放什么：任何需要真实 PostgreSQL / SMTP / IMAP 的用例（放 `tests/integration/`）。

当前阶段（骨架阶段）只有 `test_health.py`，验证 `GET /healthz` 返回 200 且
`status == "ok"`。公共夹具在 `tests/conftest.py`：`client` fixture 用应用工厂
`create_app()` 构造 TestClient。

运行：

```powershell
uv run pytest tests/unit
```

约定：本目录不放 `__init__.py`；测试文件命名 `test_*.py`，测试函数命名 `test_*`。
