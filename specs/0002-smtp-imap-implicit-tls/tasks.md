# SMTP/IMAP 直连 SSL（隐式 TLS）任务清单

| 项 | 值 |
|---|---|
| spec 编号 | `0002` |
| 对应规格 | `specs/0002-smtp-imap-implicit-tls/spec.md` |
| 对应计划 | `specs/0002-smtp-imap-implicit-tls/plan.md` |
| 状态 | `已完成` |

## 使用说明

- 任务**按依赖顺序**排列，不要跳序执行。
- 每个任务行固定四段：**编号 → 前置 → 描述 → 对应的 `FR-n` / `AC-n`**。
- 完成一项就改成 `[x]`，并在提交信息里引用 spec 编号。
- 若发现任务与 `spec.md` 冲突：**先改 spec 并重新确认**，不要默默按代码走。

---

## 阶段 1：决策记录（先于代码）

- [x] `T-1` （前置 无）`docs/research/04-security-and-review.md` 记录 TLS 校验取向：默认校验 + 显式关闭仍加密 + CA bundle，含已否决方向 — `FR-6` / `FR-10` / `AC-10`
- [x] `T-2` （前置 无）`docs/research/05-dev-and-build-environment.md` 记录 GreenMail TLS 端口与证书实测事实（自签、无 SAN、严格校验不可用） — `FR-10` / `AC-10`
- [x] `T-3` （前置 `T-1`）`specs/0001-email-forwarding-service/design.md` 配置项清单就地改写（删两个 STARTTLS 键，加模式 / 校验 / CA 项） — `FR-1` / `FR-6` / `FR-9` / `AC-10`

## 阶段 2：配置层

- [x] `T-4` （前置 `T-3`）`config.py`：二态模式字段、端口默认 465 / 993、`TLS_VERIFY` / `TLS_CA_BUNDLE`、`build_tls_context`、启动校验第 4 条判据；删除两个旧键 — `FR-1` / `FR-6` / `FR-7` / `AC-1` / `AC-6` / `AC-8`
- [x] `T-5` （前置 `T-4`）`tests/unit/test_config_tls.py`：默认值、非法取值、上下文校验开 / 关 / CA bundle、启动校验与信息不泄露、契约守卫、告警事件（先红后绿） — `AC-1` / `AC-6` / `AC-7` / `AC-8`

## 阶段 3：适配器

- [x] `T-6` （前置 `T-4`）`providers/smtp/smtplib_sender.py`：`SmtpConfig` 换 `tls_mode` + `ssl_context`、`_ResponseCheckedSmtpSSL`、删 STARTTLS 方法与步骤 — `FR-2` / `FR-3` / `AC-2` / `AC-3`
- [x] `T-7` （前置 `T-4`）`providers/imap/imapclient_fetcher.py`：`ImapConfig` 同上、`_session` 走 `ssl=` / `ssl_context=`、删 STARTTLS — `FR-4` / `FR-5` / `AC-4` / `AC-5`

## 阶段 4：装配与启动告警

- [x] `T-8` （前置 `T-6`、`T-7`）`flows/send_email.py` / `flows/fetch_inbound.py` 注入模式与上下文 — `FR-1` / `FR-2` / `FR-4` / `AC-1`
- [x] `T-9` （前置 `T-4`）`main.py`：`TLS_VERIFY=false` 时启动记结构化告警事件 — `FR-6` / `AC-7`

## 阶段 5：测试替身与端到端用例

- [x] `T-10` （前置 `T-5`）`tests/fixtures/tls/` 提交测试 CA / 服务器证书 / 私钥与重生成说明 — `AC-2` / `AC-4` / `AC-6` / `AC-7`
- [x] `T-11` （前置 `T-10`）`tests/integration/conftest.py`：`FakeSmtpServer` 可选 TLS 档位、最小 IMAP TLS 服务器、集成环境改 `*_TLS_MODE=plain` — `AC-2` / `AC-4`
- [x] `T-12` （前置 `T-11`）`tests/integration/test_tls_connectivity.py`：SMTP / IMAP 直连 SSL、不受信任证书、`TLS_VERIFY=false`、CA bundle — `AC-2` / `AC-4` / `AC-6` / `AC-7`
- [x] `T-13` （前置 `T-5`）源码扫描用例：`starttls` 调用于零命中、`.env.example` / 文档口径正确 — `AC-3` / `AC-5` / `AC-9`

## 阶段 6：文档与门禁

- [x] `T-14` （前置 `T-8`）`.env.example`、`docs/configuration.md`、`compose.dev.yaml` 注释同步 — `FR-9` / `AC-9`
- [x] `T-15` （前置 `T-12`、`T-13`、`T-14`）`bash scripts/check.sh` 全绿，回填 `spec.md` §9 与 §6 的 `AC-9` 检索范围 — `FR-11` / `AC-11`

---

## 完成前自检

- [x] `tasks.md` 中所有任务已勾选
- [x] `spec.md` 中每条 `AC-n` 都已被验证，结果已回填「验收结果」
- [x] 每条 `FR-n` 都有 `AC-n` 覆盖
- [x] 本地门禁已跑过：`ruff format --check`、`ruff check`、`mypy src`、`pytest`（带覆盖率）、`mutmut`（变异测试）
- [x] 新增的每条测试都有「能失败」的证据（先红或改坏实现证明它会红），且期望值不是由被测代码生成的
- [x] 无密钥、真实主机名 / 端口 / 账号入库
- [x] 日志与异常信息中不含邮件正文、收件人地址、凭据
- [x] 涉及不可逆决策的部分已按 `AGENTS.md` §6 更新落点（本特性 `design.md`，或跨特性底座对应的 `docs/research/` 主题册）
- [x] 该特性的 spec 目录**保留**（不要删除）
