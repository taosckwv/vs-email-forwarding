# SMTP/IMAP 直连 SSL（隐式 TLS）规格

| 项 | 值 |
|---|---|
| spec 编号 | `0002` |
| 状态 | `已确认` |
| 负责人 | 所有者 |
| 关联决策记录 | 本特性设计结论：`specs/0002-smtp-imap-implicit-tls/design.md`；跨特性底座：`docs/research/04-security-and-review.md`（对外邮件连接的 TLS 校验取向）、`docs/research/05-dev-and-build-environment.md`（GreenMail 端口与证书的实测事实）；配置项清单**就地更新**：`specs/0001-email-forwarding-service/design.md` |
| 关联 PR | 待补 |

## 1. 背景与目标

本服务的发信与收信目前只支持**明文建连 + 按需 STARTTLS 升级**：配置层只有 `SMTP_STARTTLS` / `IMAP_STARTTLS` 两个布尔开关，SMTP 侧用标准库 `smtplib.SMTP()` 建连（没有 `SMTP_SSL`），IMAP 侧硬编码 `ssl=False`（没有直连 SSL 路径）。

下游邮件网关（Coremail）的形态是唯一要对接的真实场景：SMTP 只有 **465 隐式 TLS** 可用（587 的 STARTTLS 实测不可达），IMAP 走 **993 隐式 TLS**。用明文协议去连隐式 TLS 端口会在建连阶段就失败——这不是环境网络问题，而是**代码能力缺口**：即使网络全通，收发两条业务流也不可用。

本特性的目标：增加**直连 SSL（隐式 TLS）**连接能力，让服务能按 465 / 993 打通真实邮件网关；**同时移除 STARTTLS**——TLS 路径收敛为「`ssl`（建连即 TLS）/ `plain`（全程明文）」两种，明文只用于本地联调与无 TLS 环境。不再保留「明文建连后升级」这条中间形态。

本项目仍处第一个版本，尚不存在需要保持兼容的既有部署：**本特性直接替换配置键与默认值，不保留迁移垫片**，默认值对齐真实网关口径（`ssl` + 465 / 993），本地开发靠 `.env` 覆盖为 `plain` + 3025 / 3143。

## 2. 术语

| 术语 | 含义 |
|---|---|
| 隐式 TLS（直连 SSL） | TCP 建连后**立即**进行 TLS 握手，协商完成才开始 SMTP / IMAP 会话；SMTPS 465、IMAPS 993 是典型端口 |
| STARTTLS | 先以明文建连、再用协议内命令升级为 TLS；**本特性后不再支持这条路径**，代码里也不再有入口 |
| 连接模式二态 | 本特性引入的配置取值：`plain`（全程明文，本地联调）/ `ssl`（建连即 TLS，对接真实网关） |
| 信任库 | 校验证书链时使用的 CA 集合：未配置时为操作系统 / 运行环境的默认信任库 |
| CA bundle | 一个 PEM 文件，内含一个或多个受信任的 CA 证书；用于信任私有 CA 或自签网关 |
| 校验开关 | 单一配置项 `TLS_VERIFY`：控制 TLS 连接是否校验证书链与主机名；默认开启，显式关闭时连接**仍然加密**、只是不验证对端身份 |
| 本地替身服务 | 测试里在进程内启动的真实协议服务器（真实 TCP 与真实 TLS），用于替代外部邮件服务 |

## 3. 用户故事

- `US-1` 作为部署者，我希望把 SMTP 配成 465、把 IMAP 配成 993 的直连 SSL 后服务能直接连上，以便对接真实邮件网关收发邮件。
- `US-2` 作为开发者，我希望本地联调仍能用 GreenMail 的明文端口（3025 / 3143，`plain` 模式），不必为了跑本地环境去搭一套 TLS 证书链。
- `US-3` 作为安全负责人，我希望对外 TLS 连接**默认校验证书与主机名**，私有 CA 能通过明确的配置项带进来；确需对接证书不可信的网关时，我能**显式**关闭校验，并且这件事会在启动日志里留痕——既不静默降级，也不因一次兼容需求放弃整条加密路径。
- `US-4` 作为运维，我希望配置写错（取值非法、CA 文件不存在）时服务**启动即失败**并给出可读的原因，而不是带病运行到第一次发信才暴露。

## 4. 功能需求

- `FR-1` 配置层提供两个二态连接模式配置项（分别对应 SMTP 与 IMAP），取值只允许 `plain` / `ssl`；两者默认值均为 `ssl`；对应端口默认值为 SMTP `465`、IMAP `993`。非法取值在构造配置时即报错；`SMTP_STARTTLS` / `IMAP_STARTTLS` 两个旧键删除。
- `FR-2` SMTP 适配器在 `ssl` 模式下**建连即完成 TLS 握手**（直连 SSL），会话中**不发送** `STARTTLS` 命令。
- `FR-3` SMTP 适配器在 `plain` 模式下不做任何 TLS；**STARTTLS 升级能力连同其代码路径一并移除**。
- `FR-4` IMAP 适配器在 `ssl` 模式下以直连 TLS 建连（`ssl=True` 语义），**不发起** STARTTLS 升级。
- `FR-5` IMAP 适配器在 `plain` 模式下不做任何 TLS；**STARTTLS 升级能力连同其代码路径一并移除**。
- `FR-6` `ssl` 模式下的对外连接**默认校验证书链与主机名**，SMTP 与 IMAP 行为一致；提供**可选** CA bundle 配置项用于私有 CA / 自签证书（未配置时用运行环境的默认信任库）；另提供**单一**校验开关 `TLS_VERIFY`（默认 `true`），显式设为 `false` 时**仍然加密**但跳过对端身份校验。关闭校验时，服务在启动阶段记一条结构化告警事件（事件名 + 受控字段），使「校验被关掉」可观察；该事件不含凭据、地址与路径。
- `FR-7` 启动校验增加一条判据：CA bundle 配置为非空但文件不可读时**拒绝启动**；错误信息只出现配置项名与路径，不出现凭据。
- `FR-8` 失败语义：TLS 握手失败、证书不受信任、主机名不匹配，以及把 `ssl` 模式误连到明文端口，一律按**连接类临时故障**处理（返回码为空、可重试）；不改变既有的重试与退避语义，也不把邮件置为永久失败，异常不抛给调用方。
- `FR-9` `.env.example` 与文档同步：465 / 993 + `ssl` 是默认口径与合法示例，「993 不要填」之类的禁止性说明删除；本地联调段给出 GreenMail 3025 / 3143 + `plain` 的覆盖写法。
- `FR-10` 决策记录按 `AGENTS.md` §6 落地：TLS 校验取向写跨特性册，配置项清单在 `specs/0001-email-forwarding-service/design.md` 就地改写，配置项的用途 / 默认值说明同步到 `docs/configuration.md`。
- `FR-11` 全门禁（`bash scripts/check.sh` 五步）通过；新增测试必须先红后绿并留下失败证据。

## 5. 非功能需求

- `NFR-1` **不新增运行时依赖**：直连 TLS 用标准库 `ssl` / `smtplib` 与既有 `imapclient` 的能力实现。
- `NFR-2` 凭据、地址、邮件正文、原始 MIME **不得**出现在日志、异常信息、返回值与测试夹具里（`AGENTS.md` §8 第 4 条）。
- `NFR-3` **离线可运行**：证书校验只读本地信任库或本地 CA 文件，不发起任何外部网络请求（`AGENTS.md` §8 第 5 条）。
- `NFR-4` 两种模式的行为都可被测试判定，且**不以 GreenMail 容器为必要条件**：容器未起时门禁不得因本特性变红。
- `NFR-5` 超时语义不变：每次外部调用的墙钟上界仍由 `SMTP_TIMEOUT_SECONDS` / `IMAP_TIMEOUT_SECONDS` 与调用侧的墙钟上界双层兜住。
- `NFR-6` 分层不变：配置只经 `config.py` 读取，`providers/` 不读环境变量，装配留在 `flows/`（`docs/architecture.md` §2）。

## 6. 验收标准

| 编号 | 覆盖 | 验收标准（可测试） |
|---|---|---|
| `AC-1` | `FR-1` | 配置层曝露两个二态字段，取值只允许 `plain` / `ssl`，默认均为 `ssl`，端口默认 465 / 993；写成第三值（如 `starttls`）时构造配置抛错；`SMTP_STARTTLS` / `IMAP_STARTTLS` 在配置对象上不存在；`uv run mypy src` 通过 |
| `AC-2` | `FR-2`、`FR-8` | 对着本地真实 TLS SMTP 服务（证书由测试 CA 签发）以 `ssl` 模式投递一封：返回成功判定、服务端捕获到完整报文，且服务端记录里**没有** `STARTTLS` 命令；同一用例换成不受信任证书时返回临时故障判定而**不抛异常** |
| `AC-3` | `FR-3` | `plain` 模式对着明文 SMTP 服务投递成功；`src/` 与 `tests/` 内检索不到可执行的 STARTTLS 升级调用路径；现有发信用例全绿 |
| `AC-4` | `FR-4` | 对着本地真实 TLS IMAP 服务以 `ssl` 模式完成「读信箱状态 + 按 UID 取回」，取回内容与投递内容一致，且会话中没有 STARTTLS 升级 |
| `AC-5` | `FR-5` | `plain` 模式的 IMAP 用例行为不变；`src/` 与 `tests/` 内检索不到可执行的 STARTTLS 升级调用路径；现有拉取用例全绿 |
| `AC-6` | `FR-6` | 默认校验（`TLS_VERIFY=true`）下双向可判定：测试 CA 签发且主机名匹配 → 连接成功；CA 不受信任或主机名不匹配 → 连接失败并归入临时故障；CA bundle 配置生效（配置后上述「不受信任」用例转为成功）；配置层 `TLS_VERIFY` 默认值为 `true` |
| `AC-7` | `FR-6` | 同一不受信任证书 / 主机名不匹配的服务，在 `TLS_VERIFY=false` 时连接成功（且仍是 TLS 加密连接）；服务启动阶段产生一条结构化告警事件（断言事件名与受控字段，不断言日志文案），且该事件里不出现凭据与地址 |
| `AC-8` | `FR-7` | CA bundle 配置为非空且指向不存在的文件时，启动校验抛错并拒绝启动；错误信息只含配置项名与路径 |
| `AC-9` | `FR-9` | `.env.example` 含 465 / 993 + `ssl` 的示例与本地 GreenMail（3025 / 3143 + `plain`）的覆盖示例；`src/` 与 `tests/` 内无可执行的 `starttls` 调用路径；`src/`、`tests/`、`.env.example`、`docs/configuration.md`、`compose.dev.yaml` 检索 `SMTP_STARTTLS` / `IMAP_STARTTLS` / 「993 不要填」零命中（`specs/0002` 自身与 `mutants/`、`.agent-teams/archive/` 等生成物与历史归档除外） |
| `AC-10` | `FR-10` | `docs/research/04-security-and-review.md`、`specs/0001-email-forwarding-service/design.md` 配置项清单与 `docs/configuration.md` 均已更新，且全仓对该主题只有一份当前有效说法；GreenMail 相关实测事实按 `FR-10` 的落点登记 |
| `AC-11` | `FR-11` | `bash scripts/check.sh` 五步全绿（ruff format / ruff check / mypy / pytest + 覆盖率 / mutmut）；新增测试的失败证据写进 PR 描述；变异测试的存活清单逐条人工评审并给出结论 |

## 7. 明确的非目标

- 服务器到邮件网关 465 / 993 端口的网络连通性（如防火墙、路由），单独处理，不在本规格内。
- **不支持 STARTTLS**：连到只接受 STARTTLS 的端口（587 / 143）会失败；也不做「按端口自动选模式」这类推断——模式只由配置决定。
- 不提供按协议分开的校验开关与 CA bundle：SMTP 与 IMAP 共用一组 `TLS_VERIFY` / `TLS_CA_BUNDLE`（确需不同信任库时再回到规格改）。
- 不做连接复用、连接池或长连接保活：仍是每次调用建立一次连接。
- 不把 GreenMail 接入自动化测试（它是本地联调与人工兜底，不是门禁依赖）。
- 不改 OAuth2 / XOAUTH2 等认证方式，仍用账号口令登录。
- 不改重试次数、退避表、状态机与保留期语义。
- 不改对外 HTTP API 契约与数据库结构。

## 8. 未决问题 / 设计阶段待细化

| 编号 | 问题 | 影响 | 状态 |
|---|---|---|---|
| `Q-1` | 证书校验策略（原三选一：严格校验 + 可选 CA bundle ／ 增加「关闭校验」开关 ／ 干脆不校验） | 决定 `FR-6` 与 `AC-6` / `AC-7` 的写法 | `已确认：B`——默认**校验**；提供**单一** `TLS_VERIFY`（默认 `true`），网关证书不可信时显式关闭且**仍加密**；不采用「默认不校验」；关闭校验须在启动阶段留一条结构化告警事件 |
| `Q-2` | 【设计阶段】IMAP 侧直连 TLS 的验证方式：进程内真实 IMAP TLS 假服务器（推荐，证据最强）／ 在第三方边界做接线级测试（断言以 `ssl=True` 建连，省时）。GreenMail 已实测证书自签且无 SAN，严格校验下不可用，且会引入容器依赖，不选 | 只影响 `AC-4` / `AC-5` 的测试落点与工作量，不阻塞规格确认 | `已确认：进程内真实 IMAP TLS 假服务器` |

## 9. 验收结果

| 编号 | 验证方式（命令 / 测试名 / 步骤） | 结果 |
|---|---|---|
| `AC-1` | `tests/unit/test_config_tls.py::test_defaults_are_implicit_tls`、`test_tls_mode_rejects_unknown_values`、`test_starttls_keys_are_gone`；`uv run mypy src` | 通过 |
| `AC-2` | `tests/integration/test_tls_connectivity.py::test_smtp_ssl_delivers_over_real_tls_with_configured_ca`、`test_smtp_ssl_rejects_untrusted_certificate_as_transient`（真实 TLS 握手；服务端记录里无 STARTTLS 命令） | 通过 |
| `AC-3` | `test_smtp_plain_mode_still_delivers`、`tests/unit/test_config_tls.py::test_sources_have_no_starttls_upgrade_call`；现有发信用例全绿 | 通过 |
| `AC-4` | `tests/integration/test_tls_connectivity.py::test_imap_ssl_reads_state_and_fetches`（真实 IMAP TLS 会话，命令记录里无 STARTTLS） | 通过 |
| `AC-5` | `test_sources_have_no_starttls_upgrade_call`；现有 IMAP 拉取用例全绿 | 通过 |
| `AC-6` | `tests/unit/test_config_tls.py::test_build_tls_context_verifies_with_custom_ca`、`test_build_tls_context_uses_system_store_without_bundle`；集成用例的信任 / 不信任 CA 双向判定 | 通过 |
| `AC-7` | `test_build_tls_context_disables_verification_but_keeps_encryption`、`test_lifespan_warns_when_tls_verification_is_disabled`、`test_smtp_ssl_with_verification_disabled_still_encrypts`（告警事件只带 `tls_verify`） | 通过 |
| `AC-8` | `test_validate_startup_rejects_unreadable_ca_bundle`（信息只含 `TLS_CA_BUNDLE`，不含路径） | 通过 |
| `AC-9` | `test_env_example_documents_implicit_tls_examples`、`test_legacy_keys_and_warning_are_removed`（三个检索词 × 三个文件）、`test_sources_have_no_starttls_upgrade_call` | 通过 |
| `AC-10` | 人工核对 `docs/research/04`、`docs/research/05`、`specs/0001/design.md` 配置项清单、`docs/configuration.md`：同一主题只有一份当前说法 | 通过 |
| `AC-11` | `bash scripts/check.sh` 退出码 0（5 步全绿，变异体 717 个；复验后存活 71 条逐条评审：8 条为新增代码的**纯异常文案**变异、行为等价，其余为基线既存存活项）；5 条红证据见交付说明（新增测试均先验证过「改坏实现就会红」） | 通过 |
