# vs-email-forwarding

`vs-email-forwarding`（Python 导入包名 `vs_email_forwarding`）是**内部邮件中转服务**：DSH 会话不直接发邮件，而是调用本服务的 HTTP API 把要发送的内容交给它，由本服务负责安全审查、投递，以及回收并审查回复。

---

## 当前状态

**预研阶段已收口，正进入需求阶段。业务逻辑尚未实现。**

- 仓库里已落地的是目录分层、工程配置、规则文件，以及一个能返回 `/healthz` 的最小 FastAPI 入口。
- 四条轮询流、数据库表结构、SMTP / IMAP 对接、审查逻辑均**尚未实现**。
- **安全审查目前是「永远通过」的空实现占位**——在真实审查接入之前，不得把本服务当作内容安全边界。
- 需求阶段的第一个入口是数据模型：按 `AGENTS.md` §5 在 `specs/` 下写第一个特性的 `spec.md`。

---

## 快速开始（开发机）

```bash
uv sync                                               # 按 .python-version 准备 Python 3.13 并同步依赖
uv run uvicorn vs_email_forwarding.main:app --reload  # 本地起服务（当前只有 /healthz）
bash scripts/check.sh                                 # 一键门禁
bash scripts/dev-up.sh                                # 起开发依赖（PostgreSQL + GreenMail，需要 Docker）
```

要点：

- 项目**锁定 Python 3.13**（`.python-version`）；开发机的 `python3` 已是 3.13，但仍**一律用 `uv run` 执行**，让解释器与依赖由 uv 统一管理。
- **开发机是一台 CentOS 8 Linux 虚拟机，已装 Docker**（26.1.3，与生产对齐），因此开发依赖容器在本机起得来、`tests/integration/` 的容器依赖也能在本机跑。
- 项目命令**以仓库属主身份**执行（本仓库属主是 `tao`）；`uv` 在该用户的 PATH 上。
- 门禁固定四步：`ruff format --check` → `ruff check` → `mypy src` → `pytest`。**本仓库不跑 CI**，门禁只能自己跑，没有任何自动兜底。
- 开发依赖（PostgreSQL 与本地邮件服务器 **GreenMail**）由 `compose.dev.yaml` 与 `scripts/dev-up.*` 提供：GreenMail 同时提供 SMTP 与 IMAP，本地可离线跑通「发出去 → 被拉回来 → 关联上原邮件」的完整回路（见 `docs/research/05-dev-and-build-environment.md`）。

---

## 生产部署（现状）

生产是 **CentOS 8**；镜像**在服务器上本地构建**，**基础镜像以 tar 带入**。环境前提是**与公网隔离、可达内网镜像源**，且**运行时不得依赖任何外部网络**。

**交付与部署脚本尚未实现**——`deploy/` 目前只有一份职责 `README.md`，不要擅自新建部署脚本。

新交付形态的未定项与形态见 `docs/open-questions.md` D2，当前结论见 `docs/research/01-delivery-and-deploy.md`。

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
deploy/                    部署脚本与交付链的落点（当前只有职责 README，待实现）
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
- **部署脚本尚未实现**：新交付形态的脚本尚未落地，见 `docs/open-questions.md` D2。

---

## 许可与治理

- **许可**：MIT，全文见 `LICENSE`。
- **贡献指南**：`CONTRIBUTING.md`（正文在 `docs/contributing.md`）。
- **安全政策**：`SECURITY.md`（正文在 `docs/security.md`）。
- **版本变更**：`CHANGELOG.md`，手工维护。
