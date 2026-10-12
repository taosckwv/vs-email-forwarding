# 单元测试

放什么：不依赖数据库与网络的纯逻辑测试。用 `fastapi.testclient.TestClient` 走进程内
HTTP 调用，不启动 uvicorn、不连 PostgreSQL、不发真实邮件。

不放什么：任何需要真实 PostgreSQL / SMTP / IMAP 的用例（放 `tests/integration/`）。

当前阶段（预研阶段）只有 `test_health.py`，验证 `GET /healthz` 返回 200 且
`status == "ok"`。公共夹具在 `tests/conftest.py`：`client` fixture 用应用工厂
`create_app()` 构造 TestClient。

运行：

```bash
uv run pytest tests/unit
```

约定：本目录不放 `__init__.py`；测试文件命名 `test_*.py`，测试函数命名 `test_*`。

规则：**该写什么、期望值从哪来、怎么写才算合格，见 `docs/testing.md`**（唯一出处，本文件不复述）。
本目录属于门禁第 5 步（变异测试）的**唯一范围**——见 `pyproject.toml` 的 `[tool.mutmut]`。
