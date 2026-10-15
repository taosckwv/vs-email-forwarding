# 单元测试

放什么：不依赖数据库与网络的纯逻辑测试。用 `fastapi.testclient.TestClient` 走进程内
HTTP 调用，不启动 uvicorn、不连 PostgreSQL、不发真实邮件。

不放什么：任何需要真实 PostgreSQL / SMTP / IMAP 的用例（放 `tests/integration/`）。

现状：单测已覆盖各层，按覆盖类别归纳（只举代表文件，非全量清单）：

* **纯逻辑（`core/` 与 `constants.py`）**：六态判定、退避算术与 `4xx` / `5xx` 分类、
  `Message-ID` 生成、入站 DSN / MDN / 引用判定、常量词表守护
  （如 `test_states.py`、`test_retry_policy.py`、`test_message_id.py`、`test_inbound_sorting.py`）。
* **`db/` 不连库的形状检查**：ORM 元数据与 `CHECK` 语义、引擎与会话工厂的构造、
  仓储语句的静态检查（如 `test_models_metadata.py`、`test_repository_statements.py`）。
* **请求校验 / 契约 / 序列化**：端点请求校验、响应模型与时间序列化、OpenAPI 与
  `docs/api-contract.yaml` 逐字段比对、启动即校验三条判据
  （如 `test_request_validation.py`、`test_schemas.py`、`test_openapi_contract.py`、
  `test_settings_validation.py`）。
* **配置与常量口径的静态守护**：如 `test_config_hygiene.py`、`test_constants_consistency.py`。
* **`flows/` 的边界与调度**：逐封取写库时刻、收件人截断、中性结构转换、调度间隔确定性
  （如 `test_fetch_inbound_timing.py`、`test_scheduler_intervals.py`）。
* **接口与横切面**：`/healthz`（`test_health.py`）、审查空实现标注（`test_review_placeholder.py`）、
  日志实际输出（`test_logging_output.py`）。

公共夹具在 `tests/conftest.py`：`client` fixture 用应用工厂
`create_app()` 构造 TestClient。

运行：

```bash
uv run pytest tests/unit
```

约定：本目录不放 `__init__.py`；测试文件命名 `test_*.py`，测试函数命名 `test_*`。

规则：**该写什么、期望值从哪来、怎么写才算合格，见 `docs/testing.md`**（唯一出处，本文件不复述）。
本目录属于门禁第 5 步（变异测试）的**唯一范围**——见 `pyproject.toml` 的 `[tool.mutmut]`。
