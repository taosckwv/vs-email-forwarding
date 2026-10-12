# 贡献指南

本文档说明本仓库的协作方式。**AI 协同规则以 `AGENTS.md` 为唯一真源**，本文件只补充人类贡献者视角的流程细节；两者冲突时以 `AGENTS.md` 为准。

---

## 1. 提交规范

### 1.1 格式

采用 **Conventional Commits**，**描述用中文**，由 commitizen 在 `commit-msg` 阶段强制校验（格式错误会被 pre-commit 直接拦下）。

```
<type>(<可选 scope>): <中文描述>

<可选正文：说明"为什么"，不要复述"改了什么">

<可选脚注：关联 spec 与该特性的 `design.md`、跨特性底座关联 `docs/research/` 对应主题册，如 Refs: specs/0001-outbound-review-pipeline>
```

示例：

```
feat: 新增出站审查流
fix: 修正 IMAP UID 游标导致重复拉取
docs: 补充部署预演说明
chore: 升级 ruff 到 v0.16.x
```

### 1.2 常用 type

| type | 用途 |
|---|---|
| `feat` | 新增功能 |
| `fix` | 修复缺陷 |
| `docs` | 只改文档 |
| `refactor` | 重构，不改变外部行为 |
| `perf` | 性能优化 |
| `test` | 增删改测试 |
| `build` | 构建系统或依赖变更（`pyproject.toml`、`uv.lock`） |
| `ci` | 流水线配置（本仓库不跑 CI，极少使用） |
| `chore` | 杂项，不涉及上述类别 |
| `revert` | 回滚某次提交 |

### 1.3 硬性要求

- **一条提交只做一件事**，不要把格式化、重构和新功能混在一起。
- **正文里禁止出现邮件正文、收件人地址、账号口令、原始 MIME。** 需要用邮件定位问题时，写邮件 ID。
- 提交信息**不要**写「运行正常」「功能可用」这类无法验证的结论。

---

## 2. 分支策略

| 分支 | 用途 |
|---|---|
| `main` | 唯一长期分支，始终保持可运行、可部署 |
| `feat/<slug>` | 新特性，slug 与对应 spec 目录同名（如 `feat/0001-outbound-review-pipeline`） |
| `fix/<slug>` | 缺陷修复 |
| `docs/<slug>` | 纯文档变更 |
| `chore/<slug>` | 工程配置、依赖升级等 |

约定：

- 一律**短分支 + 小步提交**，不要长期挂着大分支。
- 从最新 `main` 切出，合并前先 rebase 到 `main`。
- 合并到 `main` 前，`scripts/check.*` 必须全绿。
- 不新建 CI workflow，`.github/` 下只有 PR / Issue 模板。

---

## 3. 规格驱动流程（强制）

任何**非平凡改动**都必须先写规格、后写代码：

1. **写规格**：在 `specs/<NNNN-英文短横线-slug>/` 下创建 `spec.md`，状态写 `草稿`（编号四位递增，如 `specs/0001-outbound-review-pipeline/`）。模板在 `specs/_template/`，**直接复制，不要新造格式**。
2. **等确认**：把 spec 交给所有者确认。写完后自己改成 `待确认`；**`待确认` → `已确认` 只能由所有者推动**。未确认前**不要写 `design.md` / `plan.md` / `tasks.md`**，更不要写实现代码。
3. **写设计、计划与任务**：**spec 确认后**，在同一目录下写 `design.md`（该特性的设计真源）、`plan.md` 与 `tasks.md`。
4. **实现**：按 `tasks.md` 顺序推进，每完成一项就勾选，并标注对应的需求编号（`FR-n`）与验收标准编号（`AC-n`）。
5. **验收**：逐条对照 `AC-n` 验证，验收结果写回 `spec.md`。

**为什么计划排在确认之后**：确认过程本身会改 spec，而设计与计划完全依附于 spec；先写就是按一份尚未定稿的需求排任务，spec 一改就得重排。

**什么是非平凡改动**：新增功能、改变既有行为、改数据库表结构、改 API 契约、改外部集成（SMTP / IMAP / 审查服务）。

**禁止事项**：

- ❌ 先写代码再补规格。
- ❌ 在 spec 确认前写 `design.md` / `plan.md` / `tasks.md`。
- ❌ 一个 spec 塞进多个不相关的特性。
- ❌ 把验收标准写成「运行正常」「功能可用」这类不可测试的表述。每一条 `AC-n` 都必须能被一条命令、一个测试或一组明确步骤判定真假。
- ❌ 实现完成后删掉 spec——它是这个特性的历史依据，**保留**。
- ❌ 在 `spec.md` / `design.md` / `plan.md` / `tasks.md` 的元信息表里写日期——进度只看状态字段，时间线不记录（见 §5 文档写作规范的「不写时间线」）。

文件命名：**目录名与文件名一律 ASCII，内容用中文。**

---

## 4. 不可逆决策必须先留下决策记录

出现下列任一情况，**先按落点留下决策记录，并在 PR 里说明**，再动手——**跨特性底座**写 `docs/research/` 下对应主题册的「结论」段，**某特性的设计结论**写该特性的 `specs/<特性>/design.md`（被否决的方向写进「已否决方向」段）：

- 新增或替换依赖（尤其是引入新的运行时依赖）；
- 修改数据库表结构或索引；
- 修改 API 契约（字段、状态枚举、端点）；
- 修改部署形态（基础镜像、进程管理、交付方式、离线策略）；
- 任何「以后改回来会很贵」的选择——包括**放宽 ruff / mypy 规则**。

落点按决策的性质选取：跨特性底座按主题选册，某特性的设计结论写该特性的 `design.md`：

| 决策主题 | 落点 |
|---|---|
| 某特性的数据模型 / 流水线 / 集成契约 / 依赖选型 | 该特性的 `specs/<特性>/design.md` |
| 交付链 / 部署 / 基础镜像 / Docker / 服务器环境 | `docs/research/01-delivery-and-deploy.md` |
| 调度 / 幂等 / 数据库 / 流水线 | `docs/research/02-runtime-and-scheduling.md` |
| 技术栈 / 依赖 / 门禁 / ruff / CI | `docs/research/03-dependencies-and-toolchain.md` |
| 审查 / 发件人身份 / 日志与信息红线 | `docs/research/04-security-and-review.md` |
| 开发机、Docker 与构建环境 | `docs/research/05-dev-and-build-environment.md` |
| 命名 / 许可 / 仓库治理 | `docs/research/06-naming-licensing-and-governance.md` |

决策记录只写**结论与理由**，不写实现细节，也不写过程与时间线。

规则：

- **同一结论只允许有一个出处，只保留当前有效版本。** 决策变了就**就地改写**那一条；被取代的旧结论、旧编号一律删除，历史由 git 承担。
- **没有编号，也没有独立的决策目录**：不要新建编号式决策文件，也不要再写 `ADR-NNNN` 这类引用。
- 与决策相关但**尚未确定**的部分登记到 `docs/open-questions.md`，不要写成结论。

---

## 5. 本地门禁（本项目不跑 CI，必须自己跑）

**没有任何自动兜底**：仓库里没有 CI workflow，也不会新建。提交前请自行跑过：

```bash
bash scripts/check.sh
```

脚本依次执行：`ruff format --check` → `ruff check` → `mypy src` → `pytest`。

**首次克隆后安装钩子**（只需一次；本机尚未安装）：

```bash
uv run pre-commit install --install-hooks
uv run pre-commit install --hook-type commit-msg
```

**手动全量检查**：

```bash
uv run pre-commit run --all-files
```

> **注意**：本机 `github.com:443` 直连被拒，而 pre-commit 首次执行要 `git fetch` 它自己的 hook 仓库，因此**在这台开发机上跑不通**。门禁以 `bash scripts/check.sh` 为准——它不依赖网络（见 `docs/research/05-dev-and-build-environment.md`）。

### 5.1 本机执行前提

开发机是 **CentOS 8 Linux 虚拟机，项目命令一律以仓库属主身份执行**；`uv` 在该用户的 PATH 上，开发依赖容器在本机起得来。完整环境事实（Docker 版本与镜像源、SELinux、Python 3.13、编码与换行符硬约定）见 `docs/research/05-dev-and-build-environment.md`，**本文件不重复**。

**pre-commit 钩子在本机装不上**（安装与运行都要 `git fetch` GitHub，而本机 `github.com` 直连被拒）；首次克隆后那两条命令在有 GitHub 访问的机器上执行。**本机的门禁一律用 `bash scripts/check.sh`。**

其他约定：

- 类型标注必须完整，`mypy src` 是门禁的一部分，**不允许**用 `# type: ignore` 兜底。
- 不要手工与 ruff 的格式化结果对抗，`ruff format` 是唯一格式标准。
- 要让门禁通过，请改代码，**不要**放宽 `pyproject.toml` 里的 lint / mypy 配置——那属于不可逆决策，先写进它的落点（本特性 `design.md` 或 `docs/research/` 对应主题册）的「结论」段。

---

## 6. 安全红线

- ❌ **严禁提交 `.env`**、任何密钥、真实主机名 / 端口 / 账号 / 口令。仓库里只允许存在 `.env.example`，且其值必须是 `change-me` 这类明显假值。
- ❌ **严禁把邮件正文、附件内容、原始 MIME、收件人地址写入日志、异常信息、提交信息、CHANGELOG 或 PR 描述**。定位问题时用邮件 ID 引用。
- ❌ 严禁为了「跑通」而绕过安全审查流，或把审查开关硬编码。
- ❌ 禁止在日志里使用 `print`——统一走 structlog。
- 生产环境**与公网隔离、可达内网镜像源；运行时不得依赖任何外部网络**：不得引入任何**运行时需要访问外部网络**（公网或内网源）的依赖或行为——模型下载、遥测上报、远程配置拉取、在线许可证校验一律禁止。新增依赖前先问自己：它在隔离环境里第一次启动会发生什么？

一旦发现凭据误提交，按 `docs/security.md` 的渠道上报，**不要开公开 Issue**。

---

## 7. PR 要求

- 必须使用 `.github/PULL_REQUEST_TEMPLATE.md`。
- 必须**填写关联的 spec 编号**（如 `specs/0001-outbound-review-pipeline`）；涉及不可逆决策时同时关联它的落点（该特性的 `design.md`，或跨特性底座的 `docs/research/` 对应主题册）。
- 描述里写清：改了什么、为什么改、如何验证（贴出跑过的命令与结果）。
- 保持 PR 小而聚焦；大改动先拆 spec、再拆 PR。
- review 通过后由维护者合并，合并方式以仓库当前历史风格为准（保持线性历史优先 rebase）。
- **不要**在 PR 里附带自动生成的格式化噪音，也不要顺手升级无关依赖。

---

## 相关

- `AGENTS.md`——AI 协作规则的唯一真源。
- `docs/README.md`——文档入口。
- `docs/research/`——跨特性底座的结论真源，按主题分册。
- `specs/<特性>/design.md`——该特性设计结论的唯一真源。
- `specs/`——规格驱动工作流的载体。
