# 架构决策记录（ADR）

本目录记录项目的**架构决策**。每个 ADR 说明一个不可逆或跨模块的决定、当时的背景、以及被否决的方案和原因。

## 编号规则

- 文件名：`<NNNN>-<ascii-slug>.md`，序号从 `0001` 起，四位数字零填充。
- 序号**只增不减、不复用**。即使某个 ADR 被废弃，它的编号也永久保留，新决策另起编号。
- slug 用英文小写连字符，描述决策主题（如 `technology-stack`、`no-ci-local-gates`）。

## 状态取值

| 状态 | 含义 |
|---|---|
| 提议中 | 已写出，等待项目所有者确认，**尚未生效** |
| 已接受 | 决策已生效，实现应当遵守 |
| 已废弃 | 决策不再适用，且没有替代者 |
| 被取代 | 有了新的 ADR 替代它，须写明「被 ADR-XXXX 取代」 |

> 注意：本项目当前的 ADR 均为**已接受**，因为其中的决策已在与项目所有者的逐轮确认中敲定。

## 什么时候该新增 ADR

**该写**：改变技术栈、数据模型、部署形态、许可证与治理方式、仓库分层、调度与并发模型、以及任何「改起来很贵」的决定。

**不该写**：某个功能的字段细节、局部重构、命名选择、bug 修复方式。这些属于 `specs/<NNNN-slug>/` 的范畴。

判断方法：如果这个决定**一旦落地就很难回退**，或者在多个模块之间形成了约束，就写 ADR。

## 流程

1. **不可逆决策必须先写 ADR，再实现。** 不允许「先实现，回头补文档」。
2. ADR 写完后请项目所有者确认；确认前状态为「提议中」，不得据此开始实现。
3. 决策一旦「已接受」，**正文不再修改**。需要变更时新增一个 ADR，并在旧 ADR 的状态行写明「被 ADR-XXXX 取代」。
4. ADR 中的事实性数字（版本号、digest、端口）若尚未核实，必须标注为待定，不得编造。

## 格式模板

```markdown
# NNNN. 标题

- 状态：已接受
- 日期：YYYY-MM-DD
- 决策者：项目所有者

## 背景
## 决策
## 后果
### 正面
### 负面 / 代价
## 备选方案与被否决的原因
## 相关
```

## 索引

| 编号 | 标题 | 状态 |
|---|---|---|
| [0001](0001-technology-stack.md) | 技术栈：Python 3.13 + uv + FastAPI | 已接受 |
| [0002](0002-single-package-src-layout.md) | 仓库形态：单包 + src 布局 | 已接受 |
| [0003](0003-license-and-governance.md) | 许可证与治理：MIT + 私有仓库按开源规范 | 已接受 |
| [0004](0004-spec-driven-ai-collaboration.md) | AI 协同开发模型：规格驱动 + AGENTS.md 唯一真源 | 已接受 |
| [0005](0005-offline-docker-deployment-on-centos8.md) | 部署形态：CentOS 8 + 完全离线 + Docker | 已接受 |
| [0006](0006-production-base-image-and-rehearsal.md) | 生产基础镜像选型与 CentOS 8 部署预演 | 已接受 |
| [0007](0007-in-process-asyncio-scheduler.md) | 进程内 asyncio 四流调度 | 已接受 |
| [0008](0008-postgresql-and-idempotent-claim.md) | PostgreSQL 与幂等抢占 | 已接受 |
| [0009](0009-review-provider-noop-first.md) | 一期安全审查为空实现占位 | 已接受 |
| [0010](0010-no-ci-local-gates.md) | 不跑 CI，门禁落在本地 | 已接受 |
| [0011](0011-chinese-punctuation-and-ruf-rules.md) | 中文标点与 ruff RUF001/002/003 规则例外 | 已接受 |
| [0012](0012-sender-identity-and-reply-routing.md) | 发件人身份与回复路由 | 已接受 |
| [0013](0013-local-build-on-production-server.md) | 交付形态变更：在生产服务器上本地构建 + 基础镜像以 tar 带入 | 已接受 |
