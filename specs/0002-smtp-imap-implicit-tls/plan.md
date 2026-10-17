# SMTP/IMAP 直连 SSL（隐式 TLS）实现计划

| 项 | 值 |
|---|---|
| spec 编号 | `0002` |
| 对应规格 | `specs/0002-smtp-imap-implicit-tls/spec.md` |
| 状态 | `已完成` |

## 1. 技术方案概述

配置层去掉两个 `*_STARTTLS` 布尔，换成 `SMTP_TLS_MODE` / `IMAP_TLS_MODE`（`plain` / `ssl`，默认 `ssl`），端口默认改 465 / 993；新增 `TLS_VERIFY`（默认 `true`）与 `TLS_CA_BUNDLE`（默认空）。校验上下文只在 `config.build_tls_context` 构建一次，由 `flows/` 装配时注入 `providers/` 的配置数据类；`providers/` 仍不读环境变量。

SMTP 侧新增 `_ResponseCheckedSmtpSSL(_ResponseCheckedSmtp, smtplib.SMTP_SSL)`，复用既有取码逻辑；IMAP 侧 `_session` 改为 `ssl=` / `ssl_context=` 构造。STARTTLS 的配置项、调用与方法全部删除。

测试侧在 `tests/fixtures/tls/` 提交一套测试 CA + 服务器证书（SAN 含 `127.0.0.1` / `localhost`，带 SKID/AKID），让进程内假 SMTP 能开 TLS 档位、并新增一个最小 IMAP TLS 服务器；用例走生产适配器，端到端验证「真握手、真校验、失败归 transient」。

被否掉的方案与理由见 `design.md` §6（保留 STARTTLS、用 `SMTP_SSL` 默认上下文、GreenMail TLS、按端口推断、适配器各自建上下文）。

## 2. 涉及文件与目录

| 文件 | 动作 | 说明 |
|---|---|---|
| `src/vs_email_forwarding/config.py` | 修改 | 模式字段、端口默认、`TLS_VERIFY` / `TLS_CA_BUNDLE`、`build_tls_context`、启动校验第 4 条判据 |
| `src/vs_email_forwarding/providers/smtp/smtplib_sender.py` | 修改 | `SmtpConfig` 契约、`_ResponseCheckedSmtpSSL`、按模式选会话类、删 STARTTLS |
| `src/vs_email_forwarding/providers/imap/imapclient_fetcher.py` | 修改 | `ImapConfig` 契约、`_session` 的直连 TLS、删 STARTTLS |
| `src/vs_email_forwarding/flows/send_email.py` | 修改 | 装配点注入模式与上下文 |
| `src/vs_email_forwarding/flows/fetch_inbound.py` | 修改 | 装配点注入模式与上下文 |
| `src/vs_email_forwarding/main.py` | 修改 | 关闭校验时的启动告警事件 |
| `tests/unit/test_config_tls.py` | 新增 | 配置默认值 / 非法值 / 上下文 / 启动校验 / 契约守卫 / 告警 |
| `tests/unit/test_settings_validation.py` | 修改 | lifespan 夹具补 `tls_verify` 告警断言所需的设置 |
| `tests/fixtures/tls/ca.pem`、`server.pem`、`server.key` | 新增 | 测试 CA 与服务器证书 / 私钥（仅测试用） |
| `tests/fixtures/tls/README.md` | 新增 | 证书来源、有效期与重生成步骤 |
| `tests/integration/conftest.py` | 修改 | `FakeSmtpServer` 的 TLS 档位、最小 IMAP TLS 服务器夹具、环境变量改 `*_TLS_MODE=plain` |
| `tests/integration/test_tls_connectivity.py` | 新增 | `AC-2` / `AC-4` / `AC-6` / `AC-7` 端到端用例 |
| `tests/integration/test_fixture_contract.py` | 修改 | `ImapConfig` 构造改为新字段 |
| `.env.example` | 修改 | 465 / 993 + `ssl` 示例、本地 `plain` 覆盖、删 STARTTLS 与「993 不要填」 |
| `docs/configuration.md` | 修改 | 配置项表：模式、端口默认、`TLS_VERIFY` / `TLS_CA_BUNDLE` |
| `docs/research/04-security-and-review.md` | 修改 | 对外邮件连接的 TLS 校验取向（默认校验 / 显式关闭 / CA bundle，含已否决方向） |
| `docs/research/05-dev-and-build-environment.md` | 修改 | GreenMail 的 TLS 端口与证书实测事实（自签、无 SAN、不可用于严格校验） |
| `specs/0001-email-forwarding-service/design.md` | 修改 | 配置项清单就地改写；正文里 STARTTLS 相关口径改为连接模式 |
| `specs/0002-smtp-imap-implicit-tls/spec.md` | 修改 | 回填验收结果（§9）；`AC-9` 检索范围写精确 |
| `compose.dev.yaml` | 修改 | 注释中的 STARTTLS 说法改为「本服务不再支持 STARTTLS」 |

## 3. 数据模型变更

无：不涉及表结构、索引与迁移。

## 4. 接口变更

无对外 HTTP API 契约变更。`/healthz` 与三个业务端点字段、状态码、枚举均不变。对外部系统的连接契约变化见 `design.md` §2 / §4。

## 5. 并发与幂等设计

不变：

- 抢占方式：仍是 `SELECT ... FOR UPDATE SKIP LOCKED` + 状态字段（`pending_messages` / `messages`）。
- 重复执行的防线：认领与结果落库语义不变；本特性只换连接形态，不碰状态机。
- 失败与重试：TLS 握手 / 证书类失败归连接类 transient（`code=None`），沿用既有 15s / 30s / 60s 退避与 4 次尝试上限。
- 终态：permanent 仍由对端返回码决定；本特性不新增终态路径。

## 6. 配置项变更

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `SMTP_TLS_MODE` | `ssl` | `plain` / `ssl`；非法值构造配置即报错 |
| `IMAP_TLS_MODE` | `ssl` | 同上 |
| `SMTP_PORT` | `465` | 由 587 改为 465 |
| `IMAP_PORT` | `993` | 不变 |
| `TLS_VERIFY` | `true` | 两个协议共用；`false` 仍加密但不验证对端 |
| `TLS_CA_BUNDLE` | 空串 | 两个协议共用的 PEM 路径；空串 = 默认信任库；不可读则拒绝启动 |
| `SMTP_STARTTLS` / `IMAP_STARTTLS` | — | **删除** |

同步更新：`.env.example`、`config.py`、`docs/configuration.md`、`specs/0001/design.md` 配置项清单。

## 7. 测试策略

| 测试 | 位置 | 覆盖的 AC |
|---|---|---|
| 配置默认值、非法取值、契约守卫（`ssl` 模式缺上下文） | `tests/unit/test_config_tls.py` | `AC-1` |
| `ssl` 模式对真实 TLS SMTP：投递成功、无 STARTTLS 命令、不受信任证书归 transient | `tests/integration/test_tls_connectivity.py` | `AC-2` |
| `plain` 模式对明文 SMTP 投递成功；全仓无 STARTTLS 调用路径 | `tests/integration/test_tls_connectivity.py`、`tests/unit/test_config_tls.py`（源码扫描） | `AC-3` |
| `ssl` 模式对真实 TLS IMAP：`read_mailbox_state` + `fetch_messages` | `tests/integration/test_tls_connectivity.py` | `AC-4` |
| `plain` 模式 IMAP 行为不变；无 STARTTLS 调用路径 | 现有拉取用例 + 源码扫描 | `AC-5` |
| 默认校验双向、CA bundle 生效、`TLS_VERIFY` 默认 true | `tests/unit/test_config_tls.py`、`tests/integration/test_tls_connectivity.py` | `AC-6` |
| `TLS_VERIFY=false` 可连不受信任证书；启动告警事件 | `tests/unit/test_config_tls.py`、`tests/integration/test_tls_connectivity.py` | `AC-7` |
| CA bundle 不可读 → 启动拒绝，信息只含配置项名 | `tests/unit/test_config_tls.py` | `AC-8` |
| `.env.example` / 文档检索零命中、示例口径正确 | `tests/unit/test_config_tls.py`（文件扫描） | `AC-9` |
| 决策记录与配置文档已更新 | 人工核对 + `AC-10` 回填 | `AC-10` |
| 全门禁 | `bash scripts/check.sh` | `AC-11` |

红证据：先写用例（配置项尚不存在 / `ssl` 模式仍按明文连），确认失败后再实现；失败输出写进 PR 描述。

## 8. 风险与缓解

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| Python 3.13 严格证书校验要求 SAN + SKID/AKID | 测试证书不可用时端到端用例全红 | 证书按 `IP:127.0.0.1` / `DNS:localhost` 签发并带 SKID/AKID，已实测可用 |
| `TLS_VERIFY=false` 被误用于生产 | 加密但不认证对端 | 启动必记 `tls_verify_disabled` 告警；文档与决策记录写清使用边界 |
| 本机 / 网关证书主机名不匹配 | `ssl` 模式连不上，误判为代码缺陷 | 失败归 transient 且日志只给错误类别；排障步骤写进 `docs/research/04` |
| 最小 IMAP TLS 服务器与真实服务器行为有差异 | 用例过、线上不过 | 只覆盖本服务实际发出的命令；真实网关联调仍需人工验证（卡片范围内） |
| mutmut 对新增 config 代码产生存活变异 | 门禁评审项 | 单测覆盖默认值 / 非法值 / 上下文分支；存活逐条评审 |

## 9. 分步实施顺序

1. 决策记录：`docs/research/04`、`docs/research/05`、`specs/0001/design.md` 配置项清单。
2. `config.py` + 单测（含红证据）。
3. `providers/smtp` + 单测契约守卫。
4. `providers/imap` + 单测契约守卫。
5. `flows/` 装配与 `main.py` 告警。
6. 测试证书、`FakeSmtpServer` TLS 档位、最小 IMAP TLS 服务器、端到端用例。
7. `.env.example`、`docs/configuration.md`、`compose.dev.yaml` 注释。
8. `bash scripts/check.sh` 全绿，回填 `spec.md` §9。

## 10. 回滚方式

代码回滚到本特性之前即可（无迁移、无数据变更）。回滚后旧的 `SMTP_STARTTLS` / `IMAP_STARTTLS` 配置恢复可用；网关侧如已按 465 / 993 部署，回滚前须把 `.env` 改回 587 / 143 + STARTTLS 口径，否则旧代码连不上隐式 TLS 端口。
