# 0001. 技术栈：Python 3.13 + uv + FastAPI

- 状态：已接受
- 日期：2026-09-29
- 决策者：项目所有者

## 背景

本服务是邮件中转服务：DSH 会话调用其 API 投递邮件内容，内容先落库，再由进程内的定时轮询流做安全审查、发信、拉取回复、审查回复。开发机是 Windows，生产是 CentOS 8 上的离线 Docker 容器（见 ADR-0005）。

技术栈需要同时满足：本机开发体验、目标 Python 版本的**寿命与依赖可用性**、以及邮件领域的库生态。

## 决策

- **Python 3.13** 作为目标版本。
- 使用 **uv** 管理：独立 CPython 解释器（不依赖系统 Python）、虚拟环境、依赖解析与锁文件（`uv.lock`）、依赖分组。仓库内用 `.python-version` 锁定版本。
- Web 框架 **FastAPI + uvicorn**；配置用 **pydantic-settings**；日志用 **structlog**。

### 关键事实依据（核查于 2026-09-29）

Python 各分支状态（来源：`devguide.python.org/versions/`）：

| 分支 | 状态 | 首次发布 | EOL |
|---|---|---|---|
| 3.12 | security（仅安全修复） | 2023-10-02 | 2028-10 |
| 3.13 | bugfix | 2024-10-07 | 2029-10 |
| 3.14 | bugfix | 2025-10-07 | 2030-10 |

- **3.12 已被否决**：已进入 security-only，不再有 bugfix 发布。
- **3.14 已被否决**：项目所有者对其稳定性不放心；且 `httpx` 0.28.1 的 classifier 只到 3.12、issue #3644「Fix compatibility with Python 3.14」**仍然 open**，而 FastAPI 的 `TestClient` 依赖 `httpx`。
- **3.13 被选中**：常规 bugfix 支持约在 **2026-10-01**（随 3.15.0 首发）结束，之后为 source-only 安全修复直至 **2029-10**，比 3.12 多一年 EOL；且本项目主力依赖在该版本上轮子齐全。

已知的环境事实：

- 本机默认 Python 是**官方 3.14.4**（`C:\Python314`），另有一个 **Microsoft Store 版 3.13**（位于 `WindowsApps` 下）。Store 版在 venv 与路径上存在已知怪癖，**不使用**；因此由 uv 安装独立的官方 CPython 3.13。
- **`uvloop` 在 Windows 上永久不可用**（历史 82 个版本零 win wheel，classifier 只有 macOS/POSIX）。生产是 Linux，`uvicorn[standard]` 会自动排除 Windows 上的 uvloop，无需特殊处理。
- 依赖兼容性抽样（用于排除版本地雷）：`pydantic` 2.13.5 锁 `pydantic-core==2.46.5`，该版本有 `cp314-win_amd64` wheel；`fastapi` 0.141.1、`uvicorn` 0.54.0、`sqlalchemy` 2.1.1、`alembic` 1.20.0、`aiosmtplib` 5.1.3、`structlog` 26.1.0 均已声明 3.14 或具备 cp314 轮子。这些事实说明整个依赖集对版本变化不敏感；3.13 的兼容面比 3.14 更宽。

## 后果

### 正面

- uv 一条命令即可对齐解释器与全部依赖，队友克隆后无需手工建 venv。
- 不依赖系统 Python：CentOS 8 的系统 Python 是 3.6.8（见 ADR-0005），在开发机也避免了 Store 版的坑。
- pydantic 校验 + FastAPI 的 OpenAPI 生成，对「DSH 调用方」这一契约面很划算。
- 生产为 Linux，`uvicorn[standard]` 可启用 uvloop 与服务端性能优化。

### 负面 / 代价

- 本机默认 Python 仍是 3.14，**存在误用风险**（直接用 `python` 跑会拿到 3.14 的环境），必须一律通过 `uv run` 执行。
- uv 与 poetry 在本机均未安装，首次需要安装。
- 3.13 也已处于 bugfix 末期（约 2026-10-01 后仅安全修复），未来需要一次升级到更高分支的迁移。

## 备选方案与被否决的原因

| 方案 | 否决原因 |
|---|---|
| Python 3.12 | 已 security-only，无 bugfix，EOL 2028-10，比 3.13 少一年 |
| Python 3.14 | 所有者对其稳定性不放心；`httpx` 未声明 3.14 支持且 issue #3644 未关闭，而测试客户端依赖它 |
| Java / Go / Node | 本机无 Go 与 .NET 工具链；邮件领域库生态以 Python 与 JS 最完整；所有者明确选择 Python |
| poetry / pip-tools / 纯 requirements.txt | 均无法像 uv 一样同时管理解释器、虚拟环境与锁文件；poetry 明显更慢 |

## 相关

- [0005](0005-offline-docker-deployment-on-centos8.md)：生产运行环境决定了「不依赖系统 Python」的硬要求
- [0006](0006-production-base-image-and-rehearsal.md)：生产容器的基础镜像同样固定 Python 3.13
- [0010](0010-no-ci-local-gates.md)：门禁脚本通过 uv 运行
