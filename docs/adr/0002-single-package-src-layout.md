# 0002. 仓库形态：单包 + src 布局

- 状态：已接受
- 日期：2026-09-29
- 决策者：项目所有者

## 背景

需要先定「一个仓库里放几个包」。这个决定影响目录分层、依赖声明方式、以及未来能否把某个部分单独发布出去。候选形态最初按 JS 生态习惯列出了 monorepo（`apps/` + `packages/`）方案，但本项目是 Python 单服务。

## 决策

- **单包仓库**，采用 **`src` 布局**：`src/vs_email_forwarding/`。
- 发行名 **`vs-email-forwarding`**，导入包 **`vs_email_forwarding`**。
- ⚠️ **这是占位名。** 项目所有者尚未最终定名；改名成本为「一次目录重命名 + 一次全局替换」，随代码量上升而增加。该未定项记录在 `docs/open-questions.md` C1。（该占位名已于 2026-09-30 定为 vs-email-forwarding，见 ADR-0014）

## 后果

### 正面

- 单一 `pyproject.toml` 与单一 `uv.lock`，`uv sync` 一条命令即可；不需要 workspace 工具链。
- `src` 布局使「当前目录下的同名目录」不会被意外导入，测试跑的是真正安装的那份代码，能更早暴露打包问题。
- 目录扁平，AI agent 定位文件快。

### 负面 / 代价

- 若未来需要对外发布 SDK、或把 SMTP / IMAP / 审查适配器拆成独立可分发包，需要重构为 uv workspace 多包结构。
- 包名是占位名，越晚定越贵。

## 备选方案与被否决的原因

| 方案 | 否决原因 |
|---|---|
| monorepo（`apps/server` + `packages/core` + `packages/adapters-*` + `packages/sdk` + `packages/shared`） | 该形态的价值在于「多制品 + 生态贡献 + 改协议/改 SDK/改文档在一个改动里原子完成」，这在 JS/npm 生态里收益显著。本项目是 Python 单服务、不发布制品、不跑 CI（见 ADR-0010），引入 workspace 与其发布链路只会增加初始配置与维护面，收益接近于零。 |
| 多仓库（服务 / SDK / 适配器各自独立） | 跨仓库改动需要多次提交且无法原子化；本项目不跑 CI，没有自动化机制保证跨仓库一致，失配风险无人兜底。 |

## 相关

- [0001](0001-technology-stack.md)：包管理工具选 uv，单包下只需一份锁文件
- [0004](0004-spec-driven-ai-collaboration.md)：目录职责表是 AI 协同的边界依据
- [0010](0010-no-ci-local-gates.md)：不跑 CI 使「多仓库一致性」失去兜底
