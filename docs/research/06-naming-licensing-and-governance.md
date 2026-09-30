# 06 命名、许可与仓库治理

> **本册是「项目命名、仓库形态、许可与治理」的当前有效结论真源。**
> 只写：当前结论、当前实测证据、已否决方向、落地位置、未定项。

## 结论

### 项目定名

- 发行名 **`vs-email-forwarding`**，Python 导入包 **`vs_email_forwarding`**，包目录 **`src/vs_email_forwarding/`**。
- 旧占位名 `mail-relay`（发行名）/ `mail_relay`（导入包与目录名）**不再使用**：不得再作为代码、配置、脚本、路径、镜像名或容器名中的标识符；本册只登记它与现名的对应关系，以免残留标识被误用。
- 定名**一次性完成**，此后不再计「改名成本」：写下的迁移、模型与接口都不必预留改名预算。

### 名称映射表

**本表是这些名称唯一的出处。** 日后若要改名（例如给导入名取更短的别名），改动点就是本表以及由它派生的字面量（目录名、模块路径、脚本默认值等）。

| 项 | 当前值 | 与发行名的关系 |
|---|---|---|
| 发行名 / 发行包名 | `vs-email-forwarding` | 基准名（`pyproject.toml` 的 `[project].name`） |
| Python 导入包与目录 | `vs_email_forwarding` / `src/vs_email_forwarding/` | **机械映射**：连字符 → 下划线（Python 导入名不能含连字符） |
| 镜像名 | `vs-email-forwarding`（例如 tag `vs-email-forwarding:0.1.0`） | 同发行名 |
| compose 项目名 | 生产 `vs-email-forwarding`；开发 `vs-email-forwarding-dev` | 同发行名（开发加 `-dev`） |
| 容器名前缀 | `vs-email-forwarding-app`、`vs-email-forwarding-postgres`；开发 `vs-email-forwarding-dev-postgres`、`vs-email-forwarding-dev-mailpit` | 发行名 + 角色后缀 |
| compose 网络名 | 生产 `vs-email-forwarding-net`；开发 `vs-email-forwarding-dev-net` | 发行名 + `-net` |
| 带前缀的环境变量 | `VS_EMAIL_FORWARDING_*`，当前实际只有 `VS_EMAIL_FORWARDING_IMAGE` | 发行名大写、连字符 → 下划线 |
| 数据库用户与默认库名 | 用户 `vs_email_forwarding`；库 `vs_email_forwarding`（开发 `vs_email_forwarding_dev`、测试 `vs_email_forwarding_test`） | 同发行名的下划线形式 |
| Python 模块路径与 logger 名 | `vs_email_forwarding.*` | 随导入包 |

- **导入名是机械映射，不是另一次命名决策**：Python 导入名不能含连字符，这一步没有可选项。
- **前缀的边界要看清**：`VS_EMAIL_FORWARDING_*` 只覆盖「带前缀的环境变量」（当前即镜像 tag 一项）。`.env.example` 里的其余键（`APP_ENV`、`LOG_LEVEL`、`LOG_FORMAT`、`HTTP_HOST`、`HTTP_PORT`、`DATABASE_URL`、`POSTGRES_*`、`MAIL_FROM_ADDRESS`、`REPLY_TO_ADDRESS`、`SMTP_*`、`IMAP_*`、`POLL_*`）**不带该前缀**，命名不受本表约束。

### 仓库形态：单包仓库 + `src` 布局

- **单包仓库 + `src` 布局**：代码在 `src/vs_email_forwarding/`，单一 `pyproject.toml` 与单一 `uv.lock`，`uv sync` 一条命令即可，不需要 workspace 工具链。
- 目录扁平，AI 与人都能快速定位文件；`src` 布局下「当前目录下的同名目录」不会被意外导入。
- **`src` 布局的收益**：测试跑的是**已安装的那份代码**，能更早暴露打包问题。
- **代价**：将来若要对外发布 SDK，或把 SMTP / IMAP / 审查适配器拆成独立可分发包，**需要重构成 uv workspace 多包结构**。
- 该形态以「不发布制品、不跑 CI」为前提，见下。

### 许可证与治理

- **许可证：MIT**，仓库内放完整 `LICENSE` 文本；`pyproject.toml` 以 `license = "MIT"` 与 `license-files = ["LICENSE"]` 声明。
- **仓库私有，但完全按开源规范治理**；托管平台为 GitHub；**制品暂不发布**到任何公共或内部 registry（`pyproject.toml` 亦标注 `Private :: Do Not Upload`）。
- MIT 管的是使用许可，与仓库可见性无关，因此「私有仓库 + MIT」并不矛盾；私有阶段按开源规范治理的收益是**延迟兑现**的（要等到公开才体现）。
- **MIT 的实际含义（所有者已被告知）**：MIT 只声明使用许可、不限制用途与分发方式，因此它意味着**将来一旦公开，任何人（包括闭源商业使用）都可以使用本项目**；反过来，若最终确认永不公开，正确的许可证就是 **Proprietary / All rights reserved**（见下「两条条件性结论」——两者是同一条结论的两面，不矛盾）。
- **治理文件清单保持齐全**：`README.md`、`CONTRIBUTING.md`、`SECURITY.md`、`CHANGELOG.md`、`.github/PULL_REQUEST_TEMPLATE.md`、`.github/ISSUE_TEMPLATE/`。
- 私有阶段这些社区治理文件不会带来外部贡献者，其价值在于**流程约束**与**将来公开时的零返工**——提交历史从第一次提交起就符合 Conventional Commits。

### 提交、PR 与版本规范

- 提交信息用 **Conventional Commits**，**描述用中文**，由 **commitizen 在 `commit-msg` 阶段强制校验**，格式错误会被 pre-commit 直接拦下。
- 一条提交只做一件事，不要把格式化、重构与新功能混在一起；正文里不得出现邮件正文、收件人地址、账号口令、原始 MIME；不要写「运行正常」「功能可用」这类不可验证的结论。
- PR 必须使用仓库模板 `.github/PULL_REQUEST_TEMPLATE.md`，并**填写关联的 spec 编号**；涉及不可逆决策时同时关联 `docs/research/` 下对应的主题册。
- **版本号与 CHANGELOG 手工维护**，遵循语义化版本；commitizen 只负责提交信息校验与辅助发版，不替代人工汇总。
- 项目不跑 CI：门禁靠 pre-commit 与本地一键脚本，没有任何自动兜底。

### CHANGELOG 现状

- `CHANGELOG.md` 是 **Keep a Changelog** 骨架（`Unreleased` 下的「新增 / 变更 / 修复」三段），当前三段内容全部为**「（暂无）」**。
- **保留框架，等首次发版再填**；不引入自动发版工具。

### 两条条件性结论

- **若项目最终确认永不公开**，正确的许可证是 **Proprietary / All rights reserved**，而不是 MIT；届时需要更新本册，并同步去掉社区治理文件。
- **MIT 不含专利授权条款**，若法务在企业内部正式采用时提出要求，可能需要换成 **Apache-2.0**。

## 实测证据

- `pyproject.toml`：`[project].name = "vs-email-forwarding"`、`version = "0.1.0"`、`license = "MIT"`、`license-files = ["LICENSE"]`、classifier 含 `Private :: Do Not Upload`；`[tool.ruff.lint.isort]` 的 `known-first-party = ["vs_email_forwarding"]`；`[tool.commitizen]` 为 `cz_conventional_commits`、`tag_format = "v$version"`、`version_files = ["pyproject.toml:version"]`。
- `LICENSE`：MIT 全文，版权行署名为维护者 `sckwv`。
- `CHANGELOG.md`：Keep a Changelog 中文版骨架（1.1.0），`[Unreleased]` 下三段均为「（暂无）」，并写明版本号与 `pyproject.toml` 中的 `version` 由维护者手工同步。
- `.github/` 下只有 `PULL_REQUEST_TEMPLATE.md` 与 `ISSUE_TEMPLATE/feature_request.md`、`ISSUE_TEMPLATE/bug_report.md`，**没有 `workflows/`**。
- `.pre-commit-config.yaml`：`default_install_hook_types` 含 `commit-msg`；commitizen 钩子 `stages: [commit-msg]`，参数为 `--allow-abort --commit-msg-file`。
- `compose.yaml`：`name: vs-email-forwarding`；容器名 `vs-email-forwarding-app` / `vs-email-forwarding-postgres`；网络 `vs-email-forwarding-net`；镜像引用 `${VS_EMAIL_FORWARDING_IMAGE:-vs-email-forwarding:latest}`。
- `compose.dev.yaml`：`name: vs-email-forwarding-dev`；`POSTGRES_USER: vs_email_forwarding`、`POSTGRES_DB: vs_email_forwarding_dev`；容器名 `vs-email-forwarding-dev-postgres` / `vs-email-forwarding-dev-mailpit`；卷与网络均以 `vs-email-forwarding-dev` 为前缀；文件末尾写明测试库 `vs_email_forwarding_test` 只需手工创建一次。
- `.env.example`：`DATABASE_URL`、`POSTGRES_USER`、`POSTGRES_DB` 三处默认名都是 `vs_email_forwarding`；**唯一带前缀的键是末尾的 `VS_EMAIL_FORWARDING_IMAGE`**。
- 在 `src/`、`scripts/`、`deploy/`、`compose.yaml`、`compose.dev.yaml`、`.env.example`、`pyproject.toml` 中检索不到旧名 `mail-relay` / `mail_relay` 作为有效标识符。
- `CONTRIBUTING.md` 写明 Conventional Commits + 中文描述的格式、常用 type、分支策略与本地门禁；安全策略的**正文**在 `docs/security.md`（报告渠道、威胁清单、禁止做法、空实现标注），仓库根的 `SECURITY.md` 只有 3–5 行，是内容一律指向 `docs/security.md` 的短壳。

## 已否决方向

- 继续用占位名 `mail-relay` —— 原因：占位名越晚改越贵，改名成本随代码量上升（`src/` 下现有 15 个源文件，正是成本最低的规模）。
- 为导入名另取更短的别称 —— 原因：发行名与导入名的机械映射更不易出错，也便于全局检索。
- monorepo —— 原因：不发布制品、不跑 CI，多包收益接近于零。
- 多仓库 —— 原因：不跑 CI ⇒ 没有跨仓一致性兜底。
- 公开仓库 —— 原因：内部服务，无公开需求（治理方式仍按开源规范）。
- AGPL-3.0 —— 原因：它能防「云厂商闭源白嫖」，但会**显著抬高企业采用门槛**；本项目是内部服务，用不上这一层保护，收益不值这个代价。
- 用自动发版工具生成 CHANGELOG —— 原因：本仓库不跑 CI，社区常用的自动生成方案在这里没有执行载体，版本与 CHANGELOG 改为手工维护。
- 用 `.gitkeep` 顶替职责 `README.md` —— 原因：空文件对人和 AI 都不携带信息；新建目录放 3–5 行职责 `README.md` 是本仓库 AI 协同的边界手段。

## 落地位置

| 位置 | 内容 |
|---|---|
| `pyproject.toml` | 发行名、版本、MIT 声明、classifier、first-party 包名、commitizen 配置 |
| `LICENSE` | MIT 全文 |
| `CHANGELOG.md` | Keep a Changelog 骨架，手工维护 |
| `CONTRIBUTING.md` | 提交与 PR 规范、分支策略、本地门禁、安全红线 |
| `docs/security.md` | 安全策略**正文**：报告渠道、威胁清单、禁止做法、空实现标注 |
| `SECURITY.md` | 仓库根的**短壳**（3–5 行，只指向 `docs/security.md`）：作用是让 GitHub 识别出本仓库有安全策略文件，正文不在根文件里重复 |
| `.github/PULL_REQUEST_TEMPLATE.md`、`.github/ISSUE_TEMPLATE/` | PR 与 Issue 模板 |
| `.pre-commit-config.yaml` | commitizen 的 `commit-msg` 校验钩子 |
| `compose.yaml`、`compose.dev.yaml` | compose 项目名、容器名、网络名、镜像名与数据库默认名 |
| `.env.example` | 唯一带前缀的键 `VS_EMAIL_FORWARDING_IMAGE`，以及 `POSTGRES_*` 默认名 |
| `src/vs_email_forwarding/` | 导入包目录（`src` 布局） |
| `docs/research/06-naming-licensing-and-governance.md` | 本册：名称映射表的唯一出处 |

## 未定项

详见 `docs/open-questions.md`：

- C2 服务监听地址与端口、firewalld 与端口映射
- C3 时区与时间格式

C1（最终包名与发行名）**已关闭**：结论已写入本册，不再列为未定项。
