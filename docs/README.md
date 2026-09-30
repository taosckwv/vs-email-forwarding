# 文档索引

本目录存放项目的设计与决策文档。**文档内容一律中文**；文件名与目录名一律 **ASCII**（避免 CentOS 8 的 locale、tar、docker 处理中文路径时出现编码问题）。

> **当前处于「预研阶段」**：本阶段只确认「**大体的架子与方向**」。**实现细节（表结构字段、接口字段契约、校验强度、表形态取舍、错误码分类等）一律只登记、不决定**，留到**需求阶段**。下文中标记为「计划中」的文档即属于需求阶段的产物。

## 本目录内容

| 文件 | 用途 |
|---|---|
| `README.md` | 本文件：文档索引与写作规范 |
| `open-questions.md` | **待决问题清单**：尚未决策的事项，及每条阻塞了什么 |
| `adr/README.md` | ADR（架构决策记录）的编号规则、状态取值与新增时机 |
| `adr/0001-technology-stack.md` | 技术栈：Python 3.13 + uv + FastAPI |
| `adr/0002-single-package-src-layout.md` | 仓库形态：单包 + `src/` 布局 |
| `adr/0003-license-and-governance.md` | 许可证与开源治理：MIT + 私有仓库按开源规范 |
| `adr/0004-spec-driven-ai-collaboration.md` | AI 协同开发模型：规格驱动 + `AGENTS.md` 唯一真源 |
| `adr/0005-offline-docker-deployment-on-centos8.md` | 部署形态：CentOS 8 + 完全离线 + Docker |
| `adr/0006-production-base-image-and-rehearsal.md` | 生产基础镜像选型（`python:3.13-slim-trixie`）与 CentOS 8 部署预演（`almalinux:8`） |
| `adr/0007-in-process-asyncio-scheduler.md` | 进程内 asyncio 四流调度 |
| `adr/0008-postgresql-and-idempotent-claim.md` | PostgreSQL 与幂等抢占 |
| `adr/0009-review-provider-noop-first.md` | 一期安全审查为空实现占位 |
| `adr/0010-no-ci-local-gates.md` | 不跑 CI，门禁落在本地 |
| `adr/0011-chinese-punctuation-and-ruf-rules.md` | 中文标点与 ruff RUF001/002/003 规则例外（记录例外，不是为过门禁而放宽） |
| `adr/0012-sender-identity-and-reply-routing.md` | 发件人身份与回复路由：`From` 固定取配置 + 服务端显式写 `Reply-To` |

### 计划中、尚未创建的文档

以下文档属于**需求（规格）阶段**的产物，尚未撰写（不要误认为已经存在）：`architecture.md`、`data-model.md`、`api.md`、`configuration.md`、`development.md`、`deployment.md`、`testing.md`。它们会随对应特性规格一起落地。

## 相关目录

| 位置 | 用途 |
|---|---|
| `specs/` | **规格驱动开发**的载体。每个特性一个目录 `specs/<NNNN-slug>/`，内含 `spec.md`（需求与验收标准）、`plan.md`（实现方案）、`tasks.md`（任务拆解）。流程强制为「先写规格 → 项目所有者确认 → 再实现」。 |
| `skills/` | 仓库内可复用的 AI 流程（把有固定步骤与固定验收标准的工作固化成 skill）。 |
| `AGENTS.md`（仓库根） | AI 协作规则的**唯一真源**。其他工具（如 `CLAUDE.md`）只做指向，禁止多份规则并存导致漂移。 |

## 写作规范

1. **语言**：正文全部中文。代码标识符、命令、路径、包名、外部术语保留原文。
2. **文件名**：ASCII，小写，连字符分隔（如 `in-process-asyncio-scheduler`）。
3. **ADR 与 spec 的分工**：
   - **ADR** 记录**不可逆或跨模块的决策**及其理由、被否决的备选方案。回答「为什么这样做」。
   - **spec** 记录**某个特性的需求、边界与验收标准**。回答「要做什么、怎样才算做完」。
   - 判断标准：如果这个决定改起来很贵（技术栈、数据模型、部署形态、许可证、目录分层），写 ADR；如果只是某个功能的实现细节，写 spec。
4. **不可逆决策必须先写 ADR 再实现**。
5. **不写没验证过的具体值**。例如镜像 digest、版本号、IP 与端口，未确认前一律写成占位或标注「待定」，不要编造一个看起来确定的数字。
6. **文档与代码不一致时，以代码为准**，并应当提一个修正文档的改动。

## 维护约定

- 决策变化时**不要改已接受的 ADR 正文**，而是新增一个 ADR 并在旧 ADR 上标注「被 ADR-XXXX 取代」。
- `open-questions.md` 中的问题一旦被决定，应从该文件移除，并把结论沉淀为 ADR 或 spec。
