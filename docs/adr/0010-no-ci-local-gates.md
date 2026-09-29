# 0010. 不跑 CI，门禁落在本地

- 状态：已接受
- 日期：2026-09-29
- 决策者：项目所有者

## 背景

最初的设计是「GitHub Actions 三件套门禁」：ruff（format + lint）、mypy、pytest，配合 Postgres service。但在部署形态逐轮确定的过程中，所有者明确选择**不跑 CI**。

这带来一个连锁影响：原本选定的 `python-semantic-release` 是设计成**在 CI 中**按提交历史自动打 tag、生成 CHANGELOG 与递增版本的工具，没有 CI 之后它的自动化价值基本消失。

## 决策

- **不跑 CI**，`.github/` 下**只有 PR 模板与 Issue 模板，不创建 `workflows/`**。
- 门禁全部落在本地：
  - **pre-commit**：ruff format、ruff lint、提交信息格式校验（commitizen）、以及基础文件检查（行尾、文件末尾换行、大文件等）。
  - **一键脚本**（`scripts/check.ps1` 与 `scripts/check.sh`）：ruff format --check + ruff check + `mypy src` + pytest。
- **版本与 CHANGELOG 改为手工维护**：
  - commitizen 负责**校验提交信息格式**（Conventional Commits，中文描述）；
  - `python-semantic-release` **不采用**；
  - CHANGELOG 由人工在发版时汇总，版本号遵循语义化版本。
- 提交前必须跑过一键脚本；PR 模板中要求声明已跑过门禁。

## 后果

### 正面

- 零 CI 成本与配置，不需要维护 workflow、不需要管理 secrets、不消耗 GitHub Actions 额度。
- 提交信息格式统一，为将来人工汇总 CHANGELOG 与追溯改动提供了基础。
- 门禁在本地执行的速度远快于等待远端 CI 排队。

### 负面 / 代价

- **门禁可被绕过**：`git commit --no-verify` 能跳过 pre-commit，且没有任何远端机制拦截。
- **没有自动化的兼容性验证**。这一点与部署形态直接相关：生产是 CentOS 8 上的离线 Docker（ADR-0005、ADR-0006），而 CentOS 8 的部署预演**只能人工执行**——原本可以在 CI 里加一个「在 almalinux:8 容器中安装依赖」的 job 来提前暴露问题，现在这条防线不存在了。
- 没有自动化测试的远端记录，「这个改动跑过测试了吗」只能靠信任。
- 发版时容易漏更 CHANGELOG，因为没有任何工具在提醒。

## 备选方案与被否决的原因

| 方案 | 否决原因 |
|---|---|
| GitHub Actions 跑 ubuntu-latest，并加一个 CentOS 8 / almalinux:8 兼容性 job | 能在部署前暴露容器与依赖问题，但所有者明确选择不跑 CI。 |
| 只在 ubuntu-latest 跑基本门禁、不做 CentOS 8 兼容性验证 | 相对轻量，但仍属于 CI，与所有者决定冲突。 |
| `python-semantic-release`（本地手动执行） | 无 CI 时只能人工记得去跑，收益有限；所有者选择「commitizen 校验 + 手工 CHANGELOG」这一更可控的方案。 |
| 只靠文档约定，不引入任何工具 | 成本最低，但没有任何真实拦截能力，属于「写了等于没写」。 |
| 只写脚本、不安装 git hook | 脚本可以被遗忘；hook 至少能在提交时拦一次。 |

## 相关

- [0004](0004-spec-driven-ai-collaboration.md)：门禁与规格驱动的分工
- [0006](0006-production-base-image-and-rehearsal.md)：没有 CI 后兼容性预演只能人工执行
- [0003](0003-license-and-governance.md)：提交规范与 CHANGELOG 的治理约定
