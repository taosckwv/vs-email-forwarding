# vs-email-forwarding

`vs-email-forwarding`（Python 导入包名 `vs_email_forwarding`）是**内部邮件中转服务**：DSH 会话不直接发邮件，而是调用本服务的 HTTP API 把要发送的内容交给它，由本服务负责安全审查、投递，以及回收并审查回复。

---

## 当前状态

**预研、需求、设计与实现阶段已推进到阶段 5：四条业务流、保留期清理流、仓储层、API 契约与集成测试均已落地，本地门禁五步全绿（`505 passed`、覆盖率 90%）。**

- 已实现：目录分层与工程配置、PostgreSQL 表结构与 Alembic 迁移、`/healthz` 与投递 / 回复查询 API（契约真源 `docs/api-contract.yaml`）、四条轮询流与保留期清理流、仓储层，以及 SMTP 与 IMAP 两处适配器。
- 交付链：产出侧构建（`deploy/build.sh`）与打包（`deploy/package.sh`：导出 → 归档自检 → sha256 旁路文件 → 交付清单）、消费侧迁移（`deploy/migrate.sh`）、启动（`deploy/run.sh`）与归档自检（`deploy/check_archive.py`）五件脚本已落地；**带入服务器与 `docker load` 仍是手动步骤**。
- **安全审查目前是「永远通过」的空实现占位**——在真实审查接入之前，不得把本服务当作内容安全边界。
- 实现进度与逐条验收结论见 `specs/0001-email-forwarding-service/spec.md` §9；未定项以 `docs/open-questions.md` 为准。

---

## 快速开始（开发机）

```bash
uv sync                                               # 按 .python-version 准备 Python 3.13 并同步依赖
uv run uvicorn vs_email_forwarding.main:app --reload  # 本地起服务
bash scripts/check.sh                                 # 一键门禁
bash scripts/dev-up.sh                                # 起开发依赖（PostgreSQL + GreenMail，需要 Docker）
```

要点：

- 项目**锁定 Python 3.13**（`.python-version`）；开发机的 `python3` 已是 3.13，但仍**一律用 `uv run` 执行**，让解释器与依赖由 uv 统一管理。
- **开发机是一台 CentOS 8 Linux 虚拟机，已装 Docker**（26.1.3，与生产对齐），因此开发依赖容器在本机起得来、`tests/integration/` 的容器依赖也能在本机跑。
- 项目命令**以 root 身份**执行；`uv` 装在 `/home/tao/.local/bin/`，**该目录不在 root 的 PATH 上**，需要时先 `export PATH=/home/tao/.local/bin:$PATH`。
- 门禁固定五步：`ruff format --check` → `ruff check` → `mypy src` → `pytest`（带覆盖率）→ `mutmut`（变异测试）。**本仓库不跑 CI**，门禁只能自己跑，没有任何自动兜底；测试侧规则见 `docs/testing.md`，门禁判据见 `docs/research/03-dependencies-and-toolchain.md`。
- 开发依赖（PostgreSQL 与本地邮件服务器 **GreenMail**）由 `compose.dev.yaml` 与 `scripts/dev-up.*` 提供：GreenMail 同时提供 SMTP 与 IMAP，本地可离线跑通「发出去 → 被拉回来 → 关联上原邮件」的完整回路（见 `docs/research/05-dev-and-build-environment.md`）。

---

## 生产部署（现状）

生产是 **CentOS 8**；镜像**在开发机上构建**、**以 tar 带入服务器**（构建期基础镜像按 digest 钉在 `Dockerfile` 里），服务器**不执行构建**。环境前提是**与公网隔离、可达内网镜像源**，且**运行时不得依赖任何外部网络**。

**交付脚本已落地五件**——产出侧 `deploy/build.sh`（构建应用镜像）、`deploy/package.sh`（导出 → 归档自检 → sha256 旁路文件 → 交付清单）；消费侧 `deploy/migrate.sh`（数据库迁移的唯一入口）、`deploy/run.sh`（起服务并以 `/healthz` 探活）、`deploy/check_archive.py`（交付前归档自检）。带入与 `docker load` 是**手动步骤**；判据与约束以 `docs/research/01-delivery-and-deploy.md` 为准，不要擅自另起第二套脚本。

交付链的当前结论（在哪构建、带哪些镜像、产物怎么命名）见 `docs/research/01-delivery-and-deploy.md`；**逐步操作说明**见 [`docs/packaging.md`](docs/packaging.md)（打包）与 [`docs/deployment.md`](docs/deployment.md)（带入与部署、发版、回滚）。接口怎么调见 [`docs/api.md`](docs/api.md)。

---

## 文档导航

| 入口 | 是什么 |
|---|---|
| `docs/README.md` | **文档唯一入口**：去哪儿看、怎么读、怎么写 |
| `AGENTS.md` | **AI 协作规则的唯一真源**：规则、边界、目录职责 |
| `specs/` | 规格驱动：每个特性一个目录，装需求、设计、计划、任务 |
| `docs/open-questions.md` | **未定项的唯一清单**：动手前先确认你要做的不在其中 |

**结论真源有两处**：跨特性底座在 `docs/research/` 下的六册（交付与部署、运行时与调度、依赖与工具链、安全与审查、开发与构建环境、命名、许可与治理），某特性的设计结论在该特性的 `specs/<特性>/design.md`。根 `README.md` **不再维护第二份文档索引**——文档索引只有一份，就在 `docs/README.md`。

---

## 目录结构

```
src/vs_email_forwarding/   应用源码
  api/                     HTTP 接口层：请求校验、响应序列化、依赖注入
  service/                 用例层：同步请求路径的业务编排
  flows/                   定时任务层：四条业务流与保留期清理的循环与调度器
  providers/               外部能力适配器：review / smtp / imap，各自 base.py 定接口
  schemas/                 Pydantic 请求 / 响应模型
  db/                      ORM 模型、引擎与会话、仓储层（所有 SQL 都在这）
  core/                    纯逻辑：消息 ID 生成、状态机、重试策略
  config.py                唯一的配置入口（pydantic-settings，读 .env）
tests/                     测试：unit 纯逻辑，integration 需真实 PostgreSQL / 邮件服务
migrations/                Alembic 迁移
scripts/                   本地开发与门禁脚本
docs/                      文档唯一入口、跨特性底座结论真源、未定项清单
specs/                     每个特性一个目录（spec.md / design.md / plan.md / tasks.md）
skills/                    可复用的固定流程
deploy/                    交付脚本：build.sh / package.sh / migrate.sh / run.sh / check_archive.py
```

各目录「放什么 / 不放什么」以 `AGENTS.md` §4 的目录职责表为准；**代码分层与依赖方向**见 `docs/architecture.md`。

---

## 技术栈

| 项 | 选型 |
|---|---|
| 语言 | Python 3.13（`.python-version` 锁定） |
| 依赖与解释器 | `uv` |
| Web 框架 | FastAPI + uvicorn |
| 数据库 | PostgreSQL |
| 日志 | structlog |
| 调度 | 进程内 asyncio，四条业务流外加保留期清理各自独立循环 |
| 部署 | 容器 |

理由、实测证据与已否决方向见 `docs/research/` 对应册与本特性的 `specs/0001-email-forwarding-service/design.md`，本文件不展开。

---

## 已知限制与风险

- **CentOS 8 已 EOL 且不升级**：所有者已决定保持系统不变，接受**不再有安全更新**这一后果。
- **宿主 Docker 无官方支持**：Docker 官方安装文档已不再支持 CentOS 8 作为宿主。
- **一期不支持附件。**
- **审查是空实现**：一期审查逻辑必定通过，在真实审查接入前不构成安全边界。
- **无鉴权**：服务仅内网可达，一期不做鉴权，但 API 层已预留鉴权位置。
- **带入与 `docker load` 仍是手动步骤**：脚本只产出 tar 与 sha256 旁路文件，传输、载入与载入后逐 tag 复检由人按 `deploy/README.md` 执行（刻意不脚本化，因为脚本里不写服务器地址与凭据）。

---

## 许可与治理

- **许可**：MIT，全文见 `LICENSE`。
- **贡献指南**：`CONTRIBUTING.md`（正文在 `docs/contributing.md`）。
- **安全政策**：`SECURITY.md`（正文在 `docs/security.md`）。
- **版本变更**：`CHANGELOG.md`，手工维护。
