# 贡献指南

本文档说明本仓库的协作方式。**AI 协同规则以 `AGENTS.md` 为唯一真源**，本文件只补充人类贡献者视角的流程细节；两者冲突时以 `AGENTS.md` 为准。

---

## 1. 提交规范

### 1.1 格式

采用 **Conventional Commits**，**描述用中文**，由 commitizen 在 `commit-msg` 阶段强制校验（格式错误会被 pre-commit 直接拦下）。

```
<type>(<可选 scope>): <中文描述>

<可选正文：说明"为什么"，不要复述"改了什么">

<可选脚注：关联 spec / ADR，如 Refs: specs/0001-outbound-review-pipeline>
```

示例：

```
feat: 新增出站审查流
fix: 修正 IMAP UID 游标导致重复拉取
docs: 补充离线部署预演说明
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

1. **写规格**：在 `specs/<NNNN-英文短横线-slug>/` 下创建 `spec.md`（编号四位递增，如 `specs/0001-outbound-review-pipeline/`）。模板在 `specs/_template/`，**直接复制，不要新造格式**。
2. **写计划与任务**：同一目录下创建 `plan.md` 与 `tasks.md`。
3. **等确认**：把 spec 交给所有者确认。**未确认前不要写实现代码。**
4. **实现**：按 `tasks.md` 顺序推进，每完成一项就勾选，并标注对应的需求编号（`FR-n`）与验收标准编号（`AC-n`）。
5. **验收**：逐条对照 `AC-n` 验证，验收结果写回 `spec.md`。

**什么是非平凡改动**：新增功能、改变既有行为、改数据库表结构、改 API 契约、改外部集成（SMTP / IMAP / 审查服务）。

**禁止事项**：

- ❌ 先写代码再补规格。
- ❌ 一个 spec 塞进多个不相关的特性。
- ❌ 把验收标准写成「运行正常」「功能可用」这类不可测试的表述。每一条 `AC-n` 都必须能被一条命令、一个测试或一组明确步骤判定真假。
- ❌ 实现完成后删掉 spec——它是这个特性的历史依据，**保留**。

文件命名：**目录名与文件名一律 ASCII，内容用中文。**

---

## 4. 不可逆决策必须先写 ADR

出现下列任一情况，**先在 `docs/adr/` 新增 `NNNN-*.md`**（四位递增、中文标题），再动手：

- 新增或替换依赖（尤其是引入新的运行时依赖）；
- 修改数据库表结构或索引；
- 修改 API 契约（字段、状态枚举、端点）；
- 修改部署形态（基础镜像、进程管理、交付方式、离线策略）；
- 任何「以后改回来会很贵」的选择——包括**放宽 ruff / mypy 规则**。

ADR 只记录**决策与理由**，不写实现细节。

---

## 5. 本地门禁（本项目不跑 CI，必须自己跑）

**没有任何自动兜底**：仓库里没有 CI workflow，也不会新建。提交前请自行跑过：

```powershell
./scripts/check.ps1        # Windows
bash scripts/check.sh      # Linux / CentOS 8
```

脚本依次执行：`ruff format --check` → `ruff check` → `mypy src` → `pytest`。

**首次克隆后安装钩子**（只需一次）：

```powershell
uv run pre-commit install --install-hooks
uv run pre-commit install --hook-type commit-msg
```

**手动全量检查**：

```powershell
uv run pre-commit run --all-files
```

### 5.1 已知环境问题（Windows，实测踩过）

这两条都是**环境问题，不是仓库配置问题**，重试或按下面处理即可，不要为此改仓库配置：

1. **提交时报 `` `pre-commit` not found. Did you forget to activate your virtualenv? ``**
   原因：git 钩子脚本由 pre-commit 生成，里面用 `[ -x "$INSTALL_PYTHON" ]` 判断虚拟环境解释器；在 Windows 上该值是反斜杠路径（且本仓库路径含空格与中文），git 自带的 bash 判断失败，于是回退到「在 PATH 上找 `pre-commit`」——而 `pre-commit` 只装在 `.venv\Scripts\` 里，未激活时不在 PATH 上。
   处理（任选其一）：提交前激活虚拟环境；或把 pre-commit 装到已在 PATH 上的目录：
   `python -m pip install --user pre-commit`（**版本请与 `pyproject.toml` 中锁定的一致**）。

2. **`pre-commit` 首次为钩子建环境时报 `PermissionError: [WinError 5]`**
   现象：`virtualenv` 在预热 pip 时删除 `%TEMP%\...\pip*.exe` 被拒（该文件刚写入即被杀毒软件或索引服务锁住），报错形如
   `RuntimeError: failed to build image pip because: ... PermissionError: [WinError 5] 拒绝访问`。
   处理：**直接再提交一次**即可通过（失败的那次不会留下坏环境）。
   若反复出现，把 virtualenv 的 app-data 指到一个干净目录（用户级环境变量，生效于新开的终端）：
   `VIRTUALENV_OVERRIDE_APP_DATA=%LOCALAPPDATA%\pc-venv-data`

其他约定：

- 类型标注必须完整，`mypy src` 是门禁的一部分，**不允许**用 `# type: ignore` 兜底。
- 不要手工与 ruff 的格式化结果对抗，`ruff format` 是唯一格式标准。
- 要让门禁通过，请改代码，**不要**放宽 `pyproject.toml` 里的 lint / mypy 配置——那属于不可逆决策，先补 ADR。

---

## 6. 安全红线

- ❌ **严禁提交 `.env`**、任何密钥、真实主机名 / 端口 / 账号 / 口令。仓库里只允许存在 `.env.example`，且其值必须是 `change-me` 这类明显假值。
- ❌ **严禁把邮件正文、附件内容、原始 MIME、收件人地址写入日志、异常信息、提交信息、CHANGELOG 或 PR 描述**。定位问题时用邮件 ID 引用。
- ❌ 严禁为了「跑通」而绕过安全审查流，或把审查开关硬编码。
- ❌ 禁止在日志里使用 `print`——统一走 structlog。
- 生产环境**完全离线**：不得引入任何首次启动时需要访问公网的依赖或行为（模型下载、遥测、远程配置、在线许可证校验）。

一旦发现凭据误提交，按 `SECURITY.md` 的渠道上报，**不要开公开 Issue**。

---

## 7. PR 要求

- 必须使用 `.github/PULL_REQUEST_TEMPLATE.md`。
- 必须**填写关联的 spec 编号**（如 `specs/0001-outbound-review-pipeline`）；涉及不可逆决策时同时关联 ADR 编号。
- 描述里写清：改了什么、为什么改、如何验证（贴出跑过的命令与结果）。
- 保持 PR 小而聚焦；大改动先拆 spec、再拆 PR。
- review 通过后由维护者合并，合并方式以仓库当前历史风格为准（保持线性历史优先 rebase）。
- **不要**在 PR 里附带自动生成的格式化噪音，也不要顺手升级无关依赖。
