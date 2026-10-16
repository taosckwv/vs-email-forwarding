# AGENTS.md — AI 协同开发规则（唯一真源）

> **本文件是本仓库 AI 协作规则的唯一真源。**
> `CLAUDE.md` 以及任何其他 AI 工具的配置文件只做指向，**不重复规则内容**——规则一旦存在两份，早晚会漂移，AI 行为就会不一致。

---

## 1. 项目是什么

`vs-email-forwarding` 是一个**内部邮件中转服务**：DSH 会话不直接发邮件，而是调用本服务的 HTTP API 把要发送的内容交给它，由本服务负责安全审查、投递，以及回收并审查回复。

**本文件只放规则与边界，不复述任何结论。** 主流程、技术底座、部署形态、调度与数据模型等全部结论，按主题归档在 `docs/research/` 下：

| 你要找什么 | 去哪儿看 |
|---|---|
| 交付链与生产部署（基础镜像、Docker 版本约束、服务器事实） | `docs/research/01-delivery-and-deploy.md` |
| 调度载体、幂等抢占硬约束、重试语义 | `docs/research/02-runtime-and-scheduling.md` |
| 技术栈、依赖清单、本地门禁 | `docs/research/03-dependencies-and-toolchain.md` |
| 安全审查、API 鉴权口径、日志与信息红线 | `docs/research/04-security-and-review.md` |
| 开发机、Docker 与构建环境 | `docs/research/05-dev-and-build-environment.md` |
| 命名、许可与仓库治理 | `docs/research/06-naming-licensing-and-governance.md` |
| 代码分层与依赖方向（哪一层能调哪一层、两条触发路径怎么走） | `docs/architecture.md` |
| 代码风格（函数结构、异常、异步、接口层写法的自查与评审清单） | `docs/code-style.md` |
| 写测试要守什么规则（该写什么、期望值从哪来、怎么评审、pytest 标记约定） | `docs/testing.md` |
| 某特性的设计结论（数据模型、流水线、集成契约、依赖选型） | `specs/<特性>/design.md` |

文档总入口是 `docs/README.md`；**未定项的唯一清单**是 `docs/open-questions.md`。

---

## 2. 当前阶段与产出边界

**预研、需求与设计阶段已收口，`specs/0001-email-forwarding-service/` 已实现到阶段 5**：四条业务流、保留期清理流、仓储层、API 契约与集成测试均已落地并通过本地门禁（`505 passed`、覆盖率 90%）；交付链五件脚本已落地（产出侧构建与打包、消费侧迁移 / 启动 / 归档自检），**带入服务器与 `docker load` 仍是手动步骤**（刻意不脚本化）。**设计结论写在各特性的 `specs/<特性>/design.md`，跨特性底座写在 `docs/research/` 各册，都不在 `spec.md` 里**——`spec.md` 只写需求与验收标准，其 §8 指向已拍板的设计条目。

**未定项不允许 AI 自行决定。** 完整清单以 `docs/open-questions.md` 为准（**本文件不复述那份清单**——两处各写一份，必然产生分歧）。写代码时若发现「必须现在选一个」，就**停下来问所有者**，不要用「合理的默认值」糊过去——这类默认值一旦落到表结构或接口上，改回来的代价极高。

**已确认的结论不在本文件复述**：跨特性底座见 `docs/research/` 六册，某特性的设计结论见该特性的 `specs/<特性>/design.md`（导航见 §1 的表）。本文件的职责只有两件：**规则**与**边界**。

**不得在未确认前实现仍属未定的条目**——未定项的完整清单只在 `docs/open-questions.md`，**本文件不复述它**。设计阶段那一批条目（数据模型字段、依赖选型、重试分类、审查接口形态等）**已经拍板并落入 `specs/0001-email-forwarding-service/design.md` 与 `docs/research/` 各册**，不再是未定项。

**环境硬事实（会直接阻塞工作，故在此保留一句）**：开发机是 **CentOS 8 Linux 虚拟机，跑得了 Docker**——项目命令一律**以 root 身份**执行，`uv` 需显式引入 PATH（装在 `/home/tao/.local/bin/`）；旧 Windows 开发机**已弃用**。开发机是交付链的**产出方**：镜像**在开发机上构建**（`deploy/build.sh`），应用镜像以 tar 带入服务器，**服务器不执行构建**。详见 `docs/research/05-dev-and-build-environment.md` 与 `docs/research/01-delivery-and-deploy.md`。

---

## 3. 常用命令

```bash
# 安装 / 同步依赖（按 .python-version 准备 Python 3.13）
uv sync

# 本地起服务
uv run uvicorn vs_email_forwarding.main:app --reload

# 一键门禁：ruff format --check + ruff check + mypy src + pytest（含覆盖率）+ mutmut
bash scripts/check.sh

# 起开发依赖（PostgreSQL + GreenMail），读 compose.dev.yaml
bash scripts/dev-up.sh
```

项目命令一律**以 root 身份**执行；`uv` 装在 `/home/tao/.local/bin/`，**该目录不在 root 的 PATH 上**，需要时先 `export PATH=/home/tao/.local/bin:$PATH`。

交付脚本已落地五件（产出侧 `deploy/build.sh` 构建应用镜像、`deploy/package.sh` 导出 + 归档自检 + sha256 旁路文件 + 交付清单；消费侧 `deploy/migrate.sh` 迁移入口、`deploy/run.sh` 起服务 + `/healthz` 探活、`deploy/check_archive.py` 交付前归档自检），交付链的**产出侧与消费侧都已就位**；**带入服务器与 `docker load` 仍是手动步骤**（刻意不脚本化：脚本里不写服务器地址与凭据）。**不要擅自新建部署脚本**：形态与判据以 `docs/research/01-delivery-and-deploy.md` 为准。

---

## 4. 目录职责表

| 路径 | 放什么 | 不放什么 |
|---|---|---|
| `src/vs_email_forwarding/api/` | HTTP 接口层：请求校验、响应序列化、依赖注入 | 业务编排、SQL、业务规则、直接调用 SMTP/IMAP、定时任务 |
| `src/vs_email_forwarding/service/` | 用例层：同步请求路径的业务编排（提交邮件、查询状态与回复） | HTTP 校验与序列化、SQL、定时任务 |
| `src/vs_email_forwarding/flows/` | 定时任务的入口与编排：四条业务流与保留期清理流的循环与调度器 | HTTP 路由、SQL 字符串、同步请求的用例 |
| `src/vs_email_forwarding/providers/` | 外部能力适配器：`review/`（审查）、`smtp/`、`imap/`，各自 `base.py` 定接口 | 业务状态流转 |
| `src/vs_email_forwarding/schemas/` | Pydantic 请求 / 响应模型 | ORM 模型 |
| `src/vs_email_forwarding/db/` | ORM 模型、引擎与会话、**仓储层（所有 SQL 都在这）** | 业务规则 |
| `src/vs_email_forwarding/core/` | 纯逻辑：消息 ID 生成、状态机、重试策略 | I/O 与数据库访问 |
| `src/vs_email_forwarding/constants.py` | **服务固定语义的常量（共享叶子层）**：状态取值、受控枚举集合、重试上限与退避表、丢弃原因 | 随环境变化的值（那是 `.env` 与 `config.py`）、任何 import 项目内模块的代码、判定逻辑 |
| `src/vs_email_forwarding/config.py` | 唯一的配置入口（pydantic-settings，读 `.env`） | 别处再读一次环境变量 |
| `tests/unit/` | 纯逻辑单测，不依赖数据库与网络 | 需要真实 Postgres 的用例 |
| `tests/integration/` | 需要真实 PostgreSQL / 邮件服务的用例 | 纯逻辑断言 |
| `docs/README.md` | **文档唯一入口**：人的任务式导航、AI 先读顺序、分区清单、写作与维护规范 | 结论与规则正文（分别在 `research/` 与 `AGENTS.md`） |
| `docs/research/` | **跨特性底座的结论真源**：一册一主题，只写当前结论、实测证据、已否决方向、落地位置、未定项指针 | 决策过程、时间线、被取代的旧结论、派生快照 |
| `docs/open-questions.md` | **未定项的唯一清单** | 已决定的结论（决定后移入对应主题册或该特性的 `design.md`） |
| `docs/testing.md` | **测试规范与测试说明的唯一出处**：单测 / 集成测试规则、AI 写测试的作业流程、评审检查表、pytest 标记约定 | 需求与验收标准（属 `specs/`）、逐条 `AC-n` 的测试落点（属该特性的 `plan.md`） |
| `docs/` 其余 | `architecture.md`（**代码分层与依赖方向的唯一出处**）、`code-style.md`（**代码风格与自查/评审清单的唯一出处**）、`contributing.md`、`security.md` 的正文；后续的数据模型 / API / 配置 / 部署说明 | 需求与验收标准（那属于 `specs/`） |
| `specs/` | **每个特性一个目录**的需求、设计、计划、任务（`spec.md` / `design.md` / `plan.md` / `tasks.md`，见 §5） | 实现代码 |
| `skills/` | 可复用的固定流程（见 `skills/README.md`） | 一次性任务说明 |
| `deploy/` | 部署脚本与交付链的落点（**五件已落地**：产出侧 `build.sh` 构建、`package.sh` 打包 + 归档自检 + sha256 旁路文件；消费侧 `migrate.sh` / `run.sh` / `check_archive.py`。带入与 `docker load` 是手动步骤） | 应用代码 |
| `scripts/` | 本地开发与门禁脚本 | 部署与交付脚本（放 `deploy/`） |
| `migrations/` | Alembic 迁移 | 手工 DDL |

各层的**依赖方向**（哪一层能调哪一层）见 `docs/architecture.md`；本表只定「放什么 / 不放什么」。

### 占位文件约定（新建目录必读）

Git **不能跟踪空目录**——只建文件夹的话，`git add` 之后它不会进仓库，队友克隆下来是空的，AI agent 也读不到。因此**新建目录必须在其内部放占位文件**，本仓库约定：

- **Python 包目录**（也就是含 `__init__.py` 的目录，如 `src/vs_email_forwarding/api/`）→ 放 `__init__.py`。
- **非包目录**（如 `docs/`、`specs/`、`deploy/`、`scripts/`、`tests/unit/`）→ 放一份 **3–5 行的职责 `README.md`**，写明**这层放什么、不放什么**。
- **禁止使用 `.gitkeep`**：空文件对人和 AI 都不携带任何信息；而职责 README 能让 agent 走到任意一层就知道边界在哪——这是本仓库 AI 协同设计的核心手段之一。

---

## 5. 规格驱动工作流（强制）

任何**非平凡改动**（新增功能、改变行为、改数据结构、改外部集成）都必须按顺序走完：

1. **写规格** — 在 `specs/<NNNN-英文短横线-slug>/` 下创建 `spec.md`，状态写 `草稿`。编号四位递增，slug 用英文小写短横线，例如 `specs/0001-outbound-review-pipeline/`。**文件名与目录名一律 ASCII，内容用中文。**
2. **等确认** — 把 spec 交给所有者确认。写完自己改成 `待确认`；**从 `待确认` 到 `已确认` 只能由所有者推动。未确认前不得写 `design.md` / `plan.md` / `tasks.md`，更不得写实现代码。**
3. **写设计、计划与任务** — **spec 确认后**，在同一目录下写 `design.md`（该特性的设计真源）、`plan.md` 与 `tasks.md`——与 `spec.md` 合起来是该特性的**四件套**。模板在 `specs/_template/`，**直接复制**；实例只按模板填写、**不新造结构**，模板本身的演进须经所有者裁决（见 `specs/README.md` §2）。
4. **实现** — 按 `tasks.md` 的顺序推进，每完成一项就勾选，并标注对应的需求编号（`FR-n`）与验收标准编号（`AC-n`）。
5. **验收** — 逐条对照 `AC-n` 验证，验收结果写回 `spec.md`。

> **为什么计划排在确认之后**：确认过程本身会改 spec，而计划完全依附于 spec。先写计划，等于按一份尚未定稿的需求排任务，spec 一改就要重排——这是返工的固定来源。

禁止事项：

- **禁止「先写代码再补规格」。**
- **禁止在 spec 确认前写 `design.md` / `plan.md` / `tasks.md`。**
- 禁止一个 spec 塞进多个不相关特性。
- 禁止把验收标准写成「运行正常」「功能可用」这类**不可测试**的表述。每一条 `AC-n` 都必须能被一条命令、一个测试或一组明确步骤判定真假。
- 实现完成后 **spec 保留**（它是该特性的需求依据）；需求后来被取代时**就地改到当前有效版本**，**不要新增一份「修订版 spec」**——文档只保留有效部分，历史交给 git。
- **禁止在 `spec.md` / `design.md` / `plan.md` / `tasks.md` 的元信息表里写日期**：进度只看状态字段；关于时间的记录规则见 §6 与 `docs/README.md` §4。

---

## 6. 不可逆决策必须留下决策记录

出现下列任一情况时，**先按落点留下决策记录，再动手**——**跨特性底座**写 `docs/research/` 下对应的主题册，**某特性的设计结论**写该特性的 `specs/<特性>/design.md`；两者的写法一致：结论写进「结论」段，被否决的方向写进「已否决方向」：

- 新增或替换依赖（尤其是引入新的运行时依赖）；
- 修改数据库表结构或索引；
- 修改 API 契约（字段、状态枚举、端点）；
- 修改部署形态（基础镜像、进程管理、交付方式、离线策略）；
- 任何「以后改回来会很贵」的选择。

规则：

- **同一结论只允许有一个出处，只保留当前有效版本。** 决策变了就**就地改写**那一条；**被取代的旧结论、旧编号一律删除**，不保留「历史决策记录」——历史由 git 承担。**不得形成决策膨胀**：同一个主题（或同一个特性的同一个设计点）在任何时刻只有一份说法。
- **没有编号，也没有独立的决策目录。** `docs/adr/` 已经取消：不要新建编号式决策文件，也不要再写 `ADR-NNNN` 这类引用。
- 决策记录只写**结论、实测证据、已否决方向**，不写过程与时间线。
- 与决策相关但**尚未确定**的部分登记到 `docs/open-questions.md`，**不要写成结论**。

---

## 7. 编码约定

- **代码风格的唯一出处是 `docs/code-style.md`**：写代码的 AI 在跑门禁前逐条自查，评审（含 code-review）以它为 Standards 依据逐条判定。它只收录机器查不出的风格约束；格式与静态检查见下一条，测试规则见 `docs/testing.md`，本节不复述它们。
- **类型标注必须完整**：`mypy src` 是门禁的一部分，不允许用 `# type: ignore` 兜底。
- **格式与静态检查**：ruff 负责格式化与 lint，不要手工和它对抗；提交前必须跑过 `scripts/check.*`。
- **测试必须绑定业务行为，期望值独立于被测实现**：每条测试要能说出它在验证哪条业务规则、`AC-n` 或不变量；**禁止用被测代码（含其内部函数）生成期望值**——`assert f(x) == f(x)` 这类同义反复断言恒绿，比没有测试更危险。
- **没有红过的测试不算测试**：新增测试必须留下失败证据（先写测试看到红，或改坏实现证明它会红），并写进 PR 描述。
- **禁止为了让测试变绿而改测试**：改期望值、删断言、加 `skip` / `xfail`、弱化断言（`is not None`、`raises(Exception)`）、重新生成夹具，一律按缺陷处理；**测试文件的改动是一等评审项**。
- **集成测试用真实依赖**：真实 PostgreSQL 与邮件服务由 `scripts/dev-up.sh` 提供，**不得为了「跑通」把数据库换成 mock**（理由见 `docs/research/02-runtime-and-scheduling.md`）。
- 测试的完整规范——单测 / 集成测试规则、AI 写测试的作业流程、评审检查表、pytest 标记约定——见 `docs/testing.md`（**唯一出处，本文件不复述**）。
- **日志用 structlog**，禁止 `print`。日志中不得出现邮件正文、账号口令、收件人地址（见 §8）。
- **配置只从 `vs_email_forwarding.config` 读**，禁止在模块里直接 `os.environ[...]`。所有可变项（监听地址、SMTP/IMAP 主机与账号、数据库连接串、发件人地址、轮询间隔）都收敛在 `.env`，仓库里只有 `.env.example` 占位。
- **服务的固定语义常量只从 `vs_email_forwarding.constants` 取**（状态取值、重试上限与退避表、受控枚举集合、丢弃原因）。判据是「**改了它等于改产品行为**」：这类值不属于环境配置，**不得**为它新增 `.env` 项。`constants.py` 是共享叶子层——**不得 import 任何项目内模块**，各层（含 `db/`）都可以 import 它。
- **数据库访问必须走仓储层**（`vs_email_forwarding/db/`）。用例层、流处理层与路由层**不得裸写 SQL**，也不得直接拼 ORM 查询。
- **外部能力一律通过 `providers/` 的接口访问**，不允许在业务流程里直接 `import` 具体实现库。
- 时间一律使用带时区的 `datetime`（按 UTC 存储），不要用裸的 `datetime.now()`。
- **文件编码与换行符是硬约定，不是风格偏好**——写错会让脚本直接跑不起来，而不是显示难看一点：
  - **一切文本文件一律 UTF-8 无 BOM + LF。** 仓库不含 Windows 脚本（`.ps1` / `.bat` 已删除）。
  - **换行符以 `.gitattributes` 为准，不要自行改动其中的规则**：`.sh` / `.bash` 与其余文本文件（`.py`、`.toml`、`.yaml`、`.yml`、`.json`、`.md`、`.sql`、`Dockerfile`、`.env*`）一律 `eol=lf`。`.sh` 带 CRLF 进了容器会直接报 `bad interpreter`。

---

## 8. 本项目特有的硬约束（最容易被破坏，逐条照做）

1. **四条业务流（外加保留期清理）的处理必须幂等。** 抢占任务一律用 `SELECT ... FOR UPDATE SKIP LOCKED` 配合状态字段（外加 `next_attempt_at` 控制退避）。**禁止**「先 SELECT 判断、再 UPDATE」的两步写法——即使当前只跑单实例，这条也不能放宽：这些后台任务本来就是并发的，而且未来加实例时不允许重写。
2. **10 秒是既定间隔，但不得把 `10` 写死在代码里**：轮询间隔、每批条数都必须是配置项。
3. **发信的 `From` 只能来自服务配置，绝不允许调用方指定；服务端必须显式写入 `Reply-To`，其值等于 IMAP 拉取所用的信箱。** 不显式写 `Reply-To` 时，行为会隐式依赖「`From` 恰好等于被拉取的信箱」，任何一次改动 `From` 都会静默断掉回复回路。见 `specs/0001-email-forwarding-service/design.md`。
4. **任何日志、CHANGELOG、异常信息、PR 描述都不得泄露**：邮件正文、附件内容、账号口令、收件人地址、原始 MIME。排查需要定位邮件时，**用邮件 ID 引用，不要贴正文**。
5. **与公网隔离、可达内网源**（前提表述见 `docs/research/01-delivery-and-deploy.md`，**实质约束不变**）：不得引入任何**运行时需要访问外部网络**（公网或内网源）的依赖或行为——模型下载、遥测上报、远程配置拉取、在线许可证校验一律禁止。新增依赖时必须先问自己：它在隔离环境里第一次启动会发生什么？
6. **不得把 `.env` 提交进仓库**；不得改动 `.python-version`；不得在未确认前实现 `docs/open-questions.md` 里的未定项。
7. **重试语义固定**：发送与审查最多 **3 次重试**（首次尝试 + 3 次重试，合计最多 4 次尝试），退避 15s / 30s / 60s，之后进入终态失败并记录最后一次错误。要改这套数字，先按 §6 更新 `docs/research/02-runtime-and-scheduling.md`。

---

## 9. 提交与 PR

- **提交信息：Conventional Commits 格式，描述用中文**，例如：
  - `feat: 新增出站审查流`
  - `fix: 修正 IMAP UID 游标导致重复拉取`
  - `docs: 补充部署预演说明`

  由 commitizen 在 `commit-msg` 阶段校验，格式错误会被 pre-commit 拦下。
- 提交前必须跑过 `scripts/check.*`（ruff format/lint + mypy + pytest + 覆盖率 + 变异测试）。**本项目不跑 CI，没有任何自动兜底——门禁只能自己跑。**
- PR 必须使用 `.github/PULL_REQUEST_TEMPLATE.md`，并**填写关联的 spec 编号**；涉及不可逆决策时同时关联它的落点：跨特性底座关联 `docs/research/` 下对应的主题册，某特性的设计结论关联该特性的 `specs/<特性>/design.md`。
- 本仓库**没有 CI workflow，也不要新建**。门禁全部在本地（pre-commit + 一键脚本）。

---

## 10. 不要做的事（清单）

- ❌ 不要新建任何 CI workflow，也不要在 `.github/` 下加 `workflows/`。
- ❌ 不要引入未在 `docs/research/03-dependencies-and-toolchain.md` 记录的依赖。
- ❌ 不要把 `.env`、密钥、真实主机名 / 端口 / 账号写进仓库或提交。
- ❌ 不要改 `.python-version`。
- ❌ 不要在未确认前实现 `docs/open-questions.md` 里仍未定的条目，也不要「先用默认值顶上」。
- ❌ 不要在流处理里裸写 SQL，不要绕过 `providers/` 直接调外部库。
- ❌ 不要写不可测试的验收标准，不要跳过 `specs/` 直接改代码。
- ❌ 不要为了让门禁通过而放宽 ruff / mypy 配置（改配置属于不可逆决策，要先按 §6 更新对应落点的决策段）。
- ❌ 不要为了让测试变绿而改测试：改期望值、删断言、加 `skip` / `xfail`、弱化断言、重新生成夹具，一律按缺陷处理（规则见 §7 与 `docs/testing.md`）。
- ❌ 不要让被测代码生成测试的期望值（含「先跑一次把输出粘进断言」）——那是恒绿的同义反复断言。
- ❌ 不要为了「跑通」把集成测试的真实数据库 / 邮件服务换成 mock；容器没起就起来再跑（`bash scripts/dev-up.sh`）。
- ❌ 不要在日志里打印邮件正文、收件人地址或凭据。
- ❌ 不要改动 `.gitattributes` 里的换行符规则（一切文本文件 LF）。
- ❌ 不要用 `.gitkeep` 顶替职责 `README.md`，也不要留下空目录——Git 不跟踪空目录，别人克隆下来什么都没有。

---

## 11. 遇到不确定时

按这个顺序处理：

1. 先查 `docs/open-questions.md`——很可能已经记录了。
2. 再查 `docs/research/` 下对应的主题册与该特性的 `specs/<特性>/design.md`——可能已经决策过。
3. 再查 `specs/`——可能已有相关规格。
4. 都没有，**停下来问所有者**，并把得到的答案补充回相应文件。

**宁可停下问一次，也不要猜一个不可逆的默认值。**
