# 0014. 项目定名：vs-email-forwarding

- 状态：已接受
- 日期：2026-09-30
- 决策者：项目所有者
- 依据：项目所有者定名；`AGENTS.md` §2 与 `docs/open-questions.md` C1 对该未定项的先期记录。

## 背景

自 ADR-0002 起，本项目一直使用占位名 `mail-relay`（发行名）/ `mail_relay`（Python 导入包与目录名）。ADR-0002 已写明这是占位名并记录了改名的成本结构；`AGENTS.md` §2 也把「最终包名」列为最容易因为「先用默认值顶上」而返工的条目之一，并指出：

> 改名成本 = 一次目录 rename + 一次全局替换，代码量越大越贵。

走到预研阶段结束时，仓库只有 15 个源文件、四条流尚未实现业务逻辑，是全过程中改名成本最低的时刻。所有者据此定名，一次性完成替换。

## 决策

1. **项目定名为 `vs-email-forwarding`。** 旧名 `mail-relay` / `mail_relay` 为占位名，不再使用。
2. 完整的名称映射如下（左列为旧占位名，右列为定名）：

| 项 | 旧（占位名） | 新（定名） |
|---|---|---|
| 发行名 / 镜像名 / compose 项目名 / 容器名前缀 | `mail-relay` | `vs-email-forwarding` |
| Python 导入包与目录 | `mail_relay` | `vs_email_forwarding` |
| 带前缀的 env 变量 | `MAIL_RELAY_IMAGE` | `VS_EMAIL_FORWARDING_IMAGE` |
| 数据库默认用户 / 库（含开发库、测试库） | `mail_relay` / `mail_relay_dev` / `mail_relay_test` | `vs_email_forwarding` / `vs_email_forwarding_dev` / `vs_email_forwarding_test` |

3. **导入名的推导方式是机械映射**：发行名 `vs-email-forwarding` 中的连字符按惯例替换为下划线，得到 `vs_email_forwarding`——Python 导入名不能含连字符，这一步没有可选项。**如实说明：这是机械推导，不是另一次命名决策。** 若所有者日后想要更短的别名，改动点就是本 ADR 的这张映射表，以及仓库中由它派生的字面量（目录名、模块路径、logger 名、脚本默认值等）。
4. 本次替换覆盖目录名、代码内引用、配置与 compose 文件、脚本、部署文档，以及历史 ADR 中作为路径 / 名称出现的标识符；**不涉及任何行为变更**。

## 后果

### 正面

- 未定项 C1 关闭：此后写下的迁移、模型与接口，都不必再预留一笔「改名」成本。
- 发行名、镜像名、导入包名、compose 项目名与容器名前缀、env 变量前缀、数据库默认名在仓库内保持一致，避免后续 AI 在不同文件里读到两套名字。

### 负面 / 代价

- **开发环境的 PostgreSQL 默认用户名与库名随之变化**（`vs_email_forwarding` / `vs_email_forwarding_dev` / `vs_email_forwarding_test`）。当前尚无部署实例，因此**没有数据迁移成本**。
- 旧名只保留在两处：**git 历史**，以及**历史 ADR 中说明「该占位名已于 2026-09-30 定为 vs-email-forwarding，见 ADR-0014」的括注**。仓库其余位置不再出现旧名。

## 备选方案与被否决的原因

| 方案 | 否决原因 |
|---|---|
| 继续使用占位名 `mail-relay` / `mail_relay` | **已否决**：所有者已定名；且该未定项越晚处理越贵（ADR-0002、`AGENTS.md` §2）。 |
| 为导入名另取一个比 `vs_email_forwarding` 更短的名字 | **未采纳**：所有者未提出该需求。若日后需要，改动点就是本 ADR 的映射表（见决策 3），不影响其余设计。 |

## 相关

- [0002](0002-single-package-src-layout.md)：占位名与「改名成本」表述的出处
- [0013](0013-local-build-on-production-server.md)：同日决策，交付形态变更
- `AGENTS.md` §2：已按本 ADR 更新（C1 的占位名警告已撤销）
- `docs/open-questions.md` C1：已按本 ADR 关闭
