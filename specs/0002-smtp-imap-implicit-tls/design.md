# SMTP/IMAP 直连 SSL（隐式 TLS）设计 · `specs/0002`

本文件是该特性设计结论的唯一真源：连接模式与配置项、TLS 校验策略、两个适配器的连接构造、测试替身与失败语义的结论都在这里。
需求在 `spec.md`；跨特性底座（安全红线、分层、门禁、环境事实）在 `docs/research/` 各册，本文件只给指针。

## 1. 概览

发信与收信各只有一个「连接形态」可配：`plain`（全程明文，本地联调）或 `ssl`（建连即 TLS，对接真实网关）。`ssl` 由配置层构建一个**校验证书与主机名**的 `SSLContext` 注入适配器，适配器只负责把它交给 `smtplib.SMTP_SSL` / `IMAPClient(ssl=True)`。STARTTLS 升级能力整体删除：没有配置项、没有调用路径、没有测试替身。

| 不变量 | 结论 | 理由 |
|---|---|---|
| 连接方式只由配置决定 | 模式取值只有 `plain` / `ssl`；不做「按端口推断」 | 端口与形态的隐式绑定在换端口时会静默改变安全语义 |
| TLS 校验默认开启 | `TLS_VERIFY=true`：校验链 + 主机名；关闭时仍加密 | 标准库 `SMTP_SSL` 默认**不校验**、`IMAPClient` 默认校验，不显式统一就会一条路裸奔 |
| 校验上下文单点构建 | 只在 `config.build_tls_context` 构建，适配器接收注入的 `SSLContext` | 两处各写一遍必然漂移；`providers/` 不读配置 |
| STARTTLS 不存在 | 代码、配置、文档、测试替身均无升级路径 | 卡片只要求 465 / 993 一个场景；留半条路径就要为它维护配置与用例 |
| 明文仅本地 | `plain` 模式 = 全程明文，无 TLS | 本地 GreenMail（3025 / 3143）联调需要它，且与线上共用同一套代码路径 |
| 失败归 transient | 握手 / 证书 / 主机名 / 误连明文口 → `code=None`、可重试、不抛异常 | 与既有 `OSError` 归类一致；把证书问题判永久失败会误杀网关轮换期 |

## 2. 连接模式与配置项

| 配置项 | 取值 / 默认值 | 说明 |
|---|---|---|
| `SMTP_TLS_MODE` | `plain` / `ssl`，默认 `ssl` | 发信连接形态 |
| `IMAP_TLS_MODE` | `plain` / `ssl`，默认 `ssl` | 收信连接形态 |
| `SMTP_PORT` | 默认 `465` | 隐式 TLS 端口 |
| `IMAP_PORT` | 默认 `993` | 隐式 TLS 端口 |
| `TLS_VERIFY` | 布尔，默认 `true` | 两个协议共用；`false` 时仍加密、不验证对端 |
| `TLS_CA_BUNDLE` | 路径串，默认空串 | 两个协议共用的 PEM；空串 = 用运行环境默认信任库 |

删除的旧键：`SMTP_STARTTLS`、`IMAP_STARTTLS`（无兼容垫片，项目仍是第一个版本）。`.env.example` 的示例口径：顶部段 = 465 / 993 + `ssl`；本地段 = 3025 / 3143 + `plain`。

同步更新：`.env.example`、`config.py`、`docs/configuration.md`、`specs/0001-email-forwarding-service/design.md` 的配置项清单（就地改写）。

## 3. TLS 校验与失败语义

### 3.1 上下文构建（`config.build_tls_context`）

```
context = ssl.create_default_context(cafile=TLS_CA_BUNDLE or None)
if not TLS_VERIFY:
    context.check_hostname = False
    context.verify_mode    = ssl.CERT_NONE
```

- 必须**先**关 `check_hostname` 再设 `CERT_NONE`：顺序反了标准库会抛 `ValueError`。
- 不引入 `ssl._create_unverified_context`（私有 API）；关闭校验只是同一个上下文上的两个属性。
- 结果按 `(verify, ca_bundle)` 做 `lru_cache`：适配器每 tick 装配一次，没必要反复加载系统证书。
- 启动校验（`validate_startup`）增加第 4 条判据：`TLS_CA_BUNDLE` 非空且不可读 → 拒绝启动；错误文案只出现配置项名（不出现地址 / 凭据）。
- `TLS_VERIFY=false` 时，lifespan 在启动校验之后记一条结构化告警 `tls_verify_disabled`（字段只有 `tls_verify=false`；不含地址、口令、路径）。

### 3.2 失败归类

| 情形 | 归类 | 依据 |
|---|---|---|
| 证书不受信任、主机名不匹配、握手失败 | transient，`code=None` | `ssl.SSLError` 是 `OSError` 子类，落进既有 `except OSError` 分支 |
| `ssl` 模式误连明文端口 | transient，`code=None` | 握手阶段对端回非 TLS 字节 → `ssl.SSLError` |
| 对端 SMTP 返回码拒绝 | 既有取码与分类不变 | 取码仍只在 `_ResponseCheckedSmtp._respond` |

适配器边界仍是「全量返回受控结果、不抛异常」（`providers/*/base.py` 契约）：新增路径没有新异常出口。

## 4. 两个适配器

### 4.1 SMTP（`providers/smtp/smtplib_sender.py`）

- `SmtpConfig`：`starttls: bool` 删除，改为 `tls_mode: Literal["plain", "ssl"]` + `ssl_context: ssl.SSLContext | None`；`__post_init__` 保证 `ssl` 模式必须有上下文（契约违反在构造时就报，不拖到线程里）。
- `ssl` 分支用 `_ResponseCheckedSmtpSSL(_ResponseCheckedSmtp, smtplib.SMTP_SSL)`：MRO 实测为 `[_RespSSL, _Resp, SMTP_SSL, SMTP, object]`，取码逻辑与 `_quit` 收尾复用，`SMTP_SSL.__init__` 接收显式 `context`。
- `plain` 分支用现有 `_ResponseCheckedSmtp(smtplib.SMTP)`。
- `deliver()` 删除 `_starttls` 步骤与方法；其余步骤顺序不变（连接 → 登录 → 投递 → QUIT）。

### 4.2 IMAP（`providers/imap/imapclient_fetcher.py`）

- `ImapConfig`：同样换成 `tls_mode` + `ssl_context`，`__post_init__` 同一条契约。
- `_session` 改为 `IMAPClient(host, port, ssl=config.tls_mode == "ssl", ssl_context=config.ssl_context, timeout=timeout)`，删除 `client.starttls()`。

### 4.3 装配（`flows/`）

`_build_sender` / `_build_fetcher` 从 `Settings` 读 `*_tls_mode`，`ssl` 模式下调用 `build_tls_context(settings)` 注入；`plain` 模式注入 `None`（不构建上下文，也就不碰 CA 文件）。

## 5. 测试策略

| 层次 | 做法 |
|---|---|
| 单测（`tests/unit/`） | 配置默认值与非法取值、`build_tls_context` 的校验开 / 关 / CA bundle 生效、`validate_startup` 的 CA 判据与信息不泄露、lifespan 的 `tls_verify_disabled` 告警、`SmtpConfig` / `ImapConfig` 的契约守卫 |
| 集成：SMTP | `FakeSmtpServer` 增可选 TLS（进程内真实 TLS 握手，服务端证书由测试 CA 签发）；用例仍走生产 `SmtplibSender` |
| 集成：IMAP | 进程内最小 IMAP TLS 服务器（greeting / CAPABILITY / LOGIN / SELECT / UID FETCH / LOGOUT，记录收到的命令）；用例走生产 `ImapClientFetcher` |
| 证书 | `tests/fixtures/tls/` 提交测试 CA + 服务器证书（SAN 含 `IP:127.0.0.1` 与 `DNS:localhost`、SKID/AKID；Python 3.13 的严格校验要求后者） |
| 红证据 | 先让新用例在「实现未改」的仓库上跑红（`ssl` 模式仍按明文行为失败 / 配置项不存在），再实现；证据写进 PR 描述 |

GreenMail 不进入自动化测试：证书自签且无 SAN（`docs/research/05-dev-and-build-environment.md`），严格校验下不可用，且会引入容器依赖。本地联调仍用其明文口（`plain` 模式）。

## 6. 已否决方向

- **保留 STARTTLS 做三态/两态** —— 卡片只要求 465 / 993 一个场景；留升级路径就要维护配置项、分支与测试替身，收益为零。
- **直接用 `smtplib.SMTP_SSL` 的默认上下文** —— 默认 `verify_mode=0`、`check_hostname=False`，等于加密但不认证对端；实测见 `docs/research/04-security-and-review.md`。
- **让 `SMTP_SSL` 默认行为与 IMAP 默认行为各走各的** —— 两条路默认一个校验一个不校验，线上没人说得清到底校没校验。
- **用 GreenMail 的 SMTPS / IMAPS 做集成测试** —— 证书无 SAN，严格校验下必然失败；要过只能关校验，等于让测试口径给生产开绿灯；且引入容器依赖（`NFR-4`）。详见 `docs/research/05-dev-and-build-environment.md`。
- **按端口推断连接形态** —— 换端口即静默改变安全语义，配置与行为必须一一对应。
- **为 SMTP / IMAP 分别配置校验开关与 CA** —— 单网关场景下配置组合爆炸；真有需求再回规格改。
- **在适配器内各自构建 `SSLContext`** —— 两处安全逻辑必然漂移，且 `providers/` 读配置违反分层。

## 7. 落地位置

| 路径 | 放什么 |
|---|---|
| `src/vs_email_forwarding/config.py` | 两个模式字段、端口默认值、`TLS_VERIFY` / `TLS_CA_BUNDLE`、`build_tls_context`、启动校验第 4 条判据 |
| `src/vs_email_forwarding/providers/smtp/smtplib_sender.py` | `SmtpConfig` 契约、`_ResponseCheckedSmtpSSL`、按模式选择会话类 |
| `src/vs_email_forwarding/providers/imap/imapclient_fetcher.py` | `ImapConfig` 契约、`_session` 的 `ssl` / `ssl_context` 构造 |
| `src/vs_email_forwarding/flows/send_email.py` / `fetch_inbound.py` | 装配点注入模式与上下文 |
| `src/vs_email_forwarding/main.py` | 关闭校验时的启动告警事件 |
| `tests/unit/test_config_tls.py` | 配置、上下文、启动校验、契约守卫、告警事件 |
| `tests/integration/conftest.py` | `FakeSmtpServer` 的 TLS 档位、最小 IMAP TLS 服务器夹具 |
| `tests/integration/test_tls_connectivity.py` | `AC-2` / `AC-4` / `AC-6` / `AC-7` 的端到端用例 |
| `tests/fixtures/tls/` | 测试 CA、服务器证书与私钥、重生成说明 |
| `.env.example`、`docs/configuration.md`、`docs/research/04`、`docs/research/05`、`specs/0001/design.md` | 配置口径、TLS 校验结论、GreenMail 证书事实、配置项清单 |

## 8. 未定项

无新增未定项；全部未定项仍以 `docs/open-questions.md` 为唯一清单。
