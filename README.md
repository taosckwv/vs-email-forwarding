# vs-email-forwarding

> 内部邮件中转服务：DSH 会话不直接发信，而是把邮件交给本服务，由本服务负责安全审查、投递，以及回收并审查回复。
>
> **发行名 `vs-email-forwarding`，Python 导入包名 `vs_email_forwarding`**（2026-09-30 定名，见 **`docs/adr/0014`**）。

---

## 状态声明

**当前处于预研阶段：只确认「大体的架子与方向」，业务逻辑尚未实现。**

仓库里目前只有目录骨架、工程配置、AI 协作规则、离线交付脚本，以及一个能响应 `/healthz` 的最小 FastAPI 入口。四条轮询流、数据库表结构、审查逻辑、SMTP/IMAP 对接均为待实现或待确认。

本阶段的尺度：**架子与方向要定；实现细节（表结构字段、接口字段契约、校验强度、表形态取舍、错误码分类等）只登记、不决定**，留到需求阶段。

- 尚未定案的问题清单见 **`docs/open-questions.md`**（未定项不允许自行拍默认值）。
- 特性需求与验收标准见 **`specs/`**。
- 已冻结的技术决策见 **`docs/adr/`**。
- AI 协同规则见 **`AGENTS.md`**。

---

## 系统在做什么

一句话：**一个只对内提供 HTTP 接口、自己不直接发信的邮件中转服务。**

DSH 会话通过 `POST /v1/messages` 把邮件内容交给本服务；邮件先落入**待审表**，由四条相互独立、并发运行的进程内轮询流逐步推进——先做安全审查，过审后写入**邮件表**并投递；对方回复被专用回复邮箱收下后，再由 IMAP 拉回、同样过一遍审查，最终由 `GET /v1/messages/{id}/replies` 供 DSH 会话取回。审查不通过的邮件进入**拒审表**，不投递、不外发。

```
   DSH 会话
      │  POST /v1/messages
      ▼
  待审表 (pending)
      │
      │  流① 安全审查出站邮件（敏感信息 / 病毒）
      ├── 通过 ──► 邮件表 (queued)
      └── 不通过 ─► 拒审表 (rejected)
                      │
                      │  流② 发送邮件（SMTP 投递）
                      ▼
                外部收件人
                      │  回复
                      ▼
                 专用回复邮箱
                      │
                      │  流③ 拉取入站回复（IMAP，UID 游标增量拉取）
                      ▼
                 待审表 (inbound)
                      │
                      │  流④ 安全审查回复
                      ├── 通过 ──► 邮件表 (received)
                      └── 不通过 ─► 拒审表
                                      │
                                      │  GET /v1/messages/{id}/replies
                                      ▼
                                  DSH 会话取回回复
```

四条流全部由**进程内 asyncio 任务**驱动，随 FastAPI 的 lifespan 启停，间隔相互独立、当前默认均为 **10 秒**；间隔是配置项，不硬编码。

---

## 技术栈

| 项 | 选型 |
|---|---|
| 语言 / 运行时 | Python 3.13（`requires-python = ">=3.13,<3.14"`，由 `.python-version` 锁定） |
| 依赖与解释器管理 | [`uv`](https://docs.astral.sh/uv/) |
| 构建后端 | hatchling（单包、`src` 布局） |
| Web 框架 | FastAPI + uvicorn |
| 数据库 | PostgreSQL |
| 配置 | pydantic-settings，唯一入口 `vs_email_forwarding.config` |
| 日志 | structlog（禁止 `print`） |
| 调度 | 进程内 asyncio，四条独立循环 |
| 静态检查 | ruff（format + lint）、mypy |
| 测试 | pytest + pytest-asyncio（`asyncio_mode = "auto"`） |
| 提交规范 | Conventional Commits，描述用中文，commitizen 校验 |
| 质量门禁 | pre-commit + `scripts/check.*`（**不跑 CI**） |
| 生产部署 | CentOS 8 + **与公网隔离、可达内网镜像源**（**运行时不得依赖任何外部网络**）+ Docker 容器 |

---

## 目录结构

```
.
├── src/vs_email_forwarding/        应用源码（src 布局：api / flows / providers / schemas / db / core）
├── tests/                 测试（unit 纯逻辑，integration 需真实 PostgreSQL / 邮件服务）
├── docs/                  架构、数据模型、API、配置、部署、测试说明；docs/adr/ 放架构决策记录
├── specs/                 规格驱动开发：每个特性一个 <NNNN-slug>/ 目录（spec.md / plan.md / tasks.md）
├── skills/                可复用的固定流程
├── deploy/                镜像构建、离线交付、迁移、启动脚本与预演环境
├── scripts/               本地开发与门禁脚本（开发依赖编排、一键检查）
├── migrations/            Alembic 迁移
├── .env.example           环境变量模板（真实的 .env 不入库）
└── pyproject.toml         Python 工程配置与工具配置的唯一入口
```

> 各目录「放什么 / 不放什么」以 `AGENTS.md` 的目录职责表为准。

---

## 快速开始

**前置条件**

1. Python **3.13**（`uv` 会依据 `.python-version` 自动准备，无需手工安装）。
2. [`uv`](https://docs.astral.sh/uv/) 已安装并可用。

**安装依赖**

```powershell
uv sync
```

**（可选）启动开发依赖**

需要真实 PostgreSQL（以及本地收发回路验证用的邮件服务）时：

```powershell
./scripts/dev-up.ps1        # Windows
bash scripts/dev-up.sh      # Linux / CentOS 8
```

**配置**

```powershell
Copy-Item .env.example .env   # Linux: cp .env.example .env
```

然后按 `.env` 中注释填写真实连接参数。

**本地起服务**

```powershell
uv run uvicorn vs_email_forwarding.main:app --reload
```

---

## 配置说明

所有可变配置集中在一个文件：**`.env.example`**（模板）/ **`.env`**（本地实际值）。

- 环境变量是**唯一**的配置来源：监听地址与端口、数据库连接串、发件人与回复地址、SMTP / IMAP 主机端口账号口令、四条流的轮询间隔，全部只从环境变量读取。
- 代码内部统一通过 `vs_email_forwarding.config` 读取，**禁止**在业务模块里直接 `os.environ[...]` 或再写一份默认值。
- `.env` 已被 `.gitignore` 忽略（`.env.example` 例外保留）；模板里的口令一律是 `change-me` 这类明显假值。
- SMTP / IMAP 的主机、端口、账号、口令等**实际值由所有者后续提供**，当前模板中的 `smtp.example.com` / `imap.example.com` / `change-me` 均为占位。
- 四条轮询流间隔（`POLL_*_SECONDS`）**当前均为 10 秒**。
- 变量名清单被 `src/vs_email_forwarding/config.py` 严格依赖，**改名前请同步两侧**，否则服务起不来。

---

## 质量门禁

本项目**没有 CI workflow，也不会新建**——门禁只能靠自己跑。

**一键脚本**（依次执行 `ruff format --check`、`ruff check`、`mypy src`、`pytest`）：

```powershell
./scripts/check.ps1        # Windows
bash scripts/check.sh      # Linux / CentOS 8
```

**pre-commit**（提交时自动拦下格式、lint、提交信息问题）：

```powershell
uv run pre-commit install --install-hooks
uv run pre-commit install --hook-type commit-msg
uv run pre-commit run --all-files   # 手动全量跑一次
```

提交信息由 commitizen 在 `commit-msg` 阶段校验：Conventional Commits 格式 + 中文描述，例如 `feat: 新增出站审查流`。

> 提交前请务必跑过 `scripts/check.*`。不跑 CI 就意味着没有任何自动兜底。

---

## 部署

镜像**在 CentOS 8 生产服务器上本地构建**（不再由开发机构建），**基础镜像以镜像 tar 形式带入服务器**并 `docker load`，再启动；生产 PostgreSQL 与应用同机，数据落在宿主机数据卷上。

环境前提为「**与公网隔离，但可达内网镜像源；运行时不得依赖任何外部网络**」，决策与理由见 **`docs/adr/0013`**。

详细的构建、导入、升级与回滚步骤见 **`deploy/README.md`**——部署入口全部在 `deploy/` 下，**不要另起一套部署脚本**。

---

## 已知风险

1. **测试客户端面临 `httpx` → `httpx2` 迁移。** `httpx` 0.28.1 的 PyPI classifier 只声明到 Python 3.12，但在 3.13 上**实测可用**（本项目测试已通过），因此「能不能用」的风险已排除。真正的问题在上游：FastAPI 的 `TestClient` 已发出弃用警告，提示改用 `httpx2`（`StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead`）。**现在就迁移，还是等上游正式移除 `httpx` 支持时再迁，尚未决定**（见 `docs/open-questions.md` 的 E2）。
2. **开发环境的数据库名已随定名改变。** 项目已于 2026-09-30 定名为 `vs-email-forwarding` / `vs_email_forwarding`（见 `docs/adr/0014`），**开发环境的 PostgreSQL 默认用户名与库名随之变化**（`vs_email_forwarding` / `vs_email_forwarding_dev` / `vs_email_forwarding_test`）。当前尚无部署实例、无数据迁移成本；但此前按旧名初始化过的本地数据卷需要重建或改名。
3. **CentOS 8 已 EOL。** CentOS 8 于 2021-12-31 停止维护，官方仓库已下线，镜像与系统补丁均不再更新。生产环境的前提已按 `docs/adr/0013` 更正为「**与公网隔离、可达内网镜像源**」，本条据此更正，并请注意区分两件不同的事：

   - **可达性**：内网 yum 源可用（已实测），宿主包**可以**从内网源安装与更新——此前「完全装不了包」的说法不再成立；
   - **是否存在补丁**：CentOS 8 已 EOL 意味着**上游不再产出新的安全补丁**；内网源无论多完整，也只是缓存既有内容，**解决不了「没有新补丁」这件事**。

   也就是说，缺的不是「取包的通道」，而是「上游已经不再产出补丁」；系统级安全更新的缺口仍需人工评估与手工操作来填（从内网源装宿主包另有源版本与发行版不匹配的风险，见 `docs/open-questions.md` D12）。基础镜像与依赖版本应固定，并在 `deploy/` 中记录来源。
4. **审查一期是空实现占位。** 一期审查逻辑必定通过，真实的敏感信息 / 病毒审查来源尚未定案（见 `docs/open-questions.md`），在此之前不要把它当作安全边界。
5. **数据库驱动与 IMAP 客户端尚未选型。** `asyncpg` 与 `psycopg3`、`imapclient` 与 `aioimaplib` 均未定案（见 `docs/open-questions.md` 的依赖与工具选型一节）。因此本阶段 `pyproject.toml` 的运行时依赖**只列了骨架最小集**（fastapi / uvicorn / pydantic-settings / structlog），**尚未包含任何数据库驱动与 IMAP 客户端**；选定后需先补 ADR，再补依赖。同理，`.env.example` 里的 `DATABASE_URL` 目前以 `postgresql+asyncpg://` 为占位形态，驱动定案后需同步调整。

---

## 文档索引

| 文档 | 内容 |
|---|---|
| `AGENTS.md` | AI 协同开发规则（唯一真源） |
| `docs/open-questions.md` | 尚未定案的问题清单——**动代码前先看这里** |
| `docs/adr/` | 架构决策记录（不可逆决策必须留档） |
| `specs/` | 规格驱动开发：需求、计划、任务与验收标准 |
| `docs/` | 架构、数据模型、API 契约、配置、部署与测试说明 |
| `skills/` | 可复用的固定流程 |
| `deploy/README.md` | 镜像构建与离线交付全流程 |
| `CONTRIBUTING.md` | 提交规范、规格驱动流程与本地门禁 |
| `SECURITY.md` | 漏洞报告渠道与本项目特有的安全约束 |
| `CHANGELOG.md` | 版本变更记录（手工维护） |
