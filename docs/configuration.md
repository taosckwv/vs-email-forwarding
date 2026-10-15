# 配置项说明

> 本文件是**配置项的导航与口径说明**，不另立取值结论：配置项「有哪些、叫什么、干什么用」的唯一真源是
> `../specs/0001-email-forwarding-service/design.md` 的配置项清单；服务固定语义的落点是
> `../src/vs_email_forwarding/constants.py`。表内「默认值 / 必填」两列按 `../src/vs_email_forwarding/config.py`
> 的代码实况填写（`docs/README.md` §4：文档与代码不一致时以代码为准）。
> 「键的集合」以仓库根 `.env.example` 为准，两者用脚本比对保持一致。

## `.env` 可变项

可变配置只从 `vs_email_forwarding.config` 这一个入口读取（`AGENTS.md` §7）；仓库只提交 `.env.example` 占位，
`.env` 本身不入库。「默认值」列是 `config.py` 字段的代码默认值——`.env.example` 里的示例值是给本地联调用的
假值，两者不同时在备注说明。

「必填」列的口径：`config.py` **没有任何缺了就无法构造的字段**（全部字段带默认值）；标「部署时必填」的键，
指留空 / 留假值时对应功能不可用，或被启动校验拒绝；标「compose 必填 / 部署专用」的键不归配置层读。

| 键 | 用途 | 默认值 | 必填 | 备注 |
|---|---|---|---|---|
| `APP_ENV` | 运行环境标识：dev / test / prod | `dev` | 否 | 取值严格限定，写错启动即报错 |
| `LOG_LEVEL` | 日志级别（标准库 logging 级别名） | `INFO` | 否 | |
| `LOG_FORMAT` | 日志渲染格式：console（本地）/ json（容器采集） | `console` | 否 | |
| `HTTP_HOST` | HTTP 服务监听地址 | `0.0.0.0` | 否 | |
| `HTTP_PORT` | HTTP 服务监听端口 | `8000` | 否 | |
| `DATABASE_URL` | SQLAlchemy 异步连接串（`postgresql+asyncpg://…`） | `postgresql+asyncpg://vs_email_forwarding:change-me@127.0.0.1:5432/vs_email_forwarding` | 部署时必填（指向真实库） | 代码与示例里都是明显假值；容器内被 compose 用 `POSTGRES_*` 重拼**整条覆盖**；集成测试只把它当服务器坐标，库名由夹具生成（见下文） |
| `POSTGRES_USER` | compose 里 postgres 容器 initdb 的用户 | `vs_email_forwarding` | compose 使用 | **compose 专用**，`config.py` 不读（`Settings` 为 `extra="ignore"`）；须与 `DATABASE_URL` 中的用户一致 |
| `POSTGRES_PASSWORD` | compose 里 postgres 容器 initdb 的口令 | 无（示例值 `change-me`） | **compose 必填**：compose 用 `${POSTGRES_PASSWORD:?}` 校验，不填 `docker compose up` 直接失败 | **compose 专用**，`config.py` 不读；须与 `DATABASE_URL` 中的口令一致；口令不要含 `$` `@` `:` `/`（compose 插值与 URL 解析都会出错） |
| `POSTGRES_DB` | compose 里 postgres 容器 initdb 的初始库名 | `vs_email_forwarding` | compose 使用 | **compose 专用**，`config.py` 不读；须与 `DATABASE_URL` 中的库名一致（本地开发把两处都改成 `vs_email_forwarding_dev`） |
| `MAIL_FROM_ADDRESS` | 发信 `From` 的唯一来源，调用方不可指定；其域也用于生成 `Message-ID` | `noreply@example.com` | 部署时必填有效地址 | 启动校验判据 2：必须能解析出邮件域；真实发信身份由部署提供，仓库只有占位 |
| `REPLY_TO_ADDRESS` | 服务端显式写入的 `Reply-To`，回复经它路由回本服务 | `reply@example.com` | 部署时必填（须与 `IMAP_ADDRESS` 一致） | 启动校验判据 1 / 3 兜底一致性 |
| `SMTP_HOST` | SMTP 服务器地址 | 空串 | 部署时必填（留空则发信流无法投递；不在启动校验三判据内） | 真实值由所有者提供，仓库只有假值 |
| `SMTP_PORT` | SMTP 端口 | `587` | 否 | |
| `SMTP_USERNAME` | SMTP 账号 | 空串 | 随 `SMTP_HOST` 一并提供 | |
| `SMTP_PASSWORD` | SMTP 口令 | 空串 | 随 `SMTP_HOST` 一并提供 | 绝不提交真实口令 |
| `SMTP_STARTTLS` | SMTP 是否启用 STARTTLS | `true` | 否 | 本地 GreenMail（明文口）用 `false` |
| `IMAP_HOST` | IMAP 服务器地址 | 空串 | 部署时必填（留空则入站拉取不可用；不在启动校验三判据内） | 真实值由所有者提供，仓库只有假值 |
| `IMAP_PORT` | IMAP 端口 | `993` | 否 | `.env.example` 示例填 `3143`，是本地 GreenMail 的明文口；`993` 是隐式 TLS（连上即 TLS）端口，本服务 IMAP 侧只有 `IMAP_STARTTLS` 一个 TLS 开关（`design.md` §4.5），明文连 `993` 只会在 greeting 就失败 |
| `IMAP_USERNAME` | IMAP 登录名 | 空串 | 随 `IMAP_HOST` 一并提供 | **不**作为被拉取信箱地址的默认值（登录名不保证是邮箱地址） |
| `IMAP_PASSWORD` | IMAP 口令 | 空串 | 随 `IMAP_HOST` 一并提供 | 绝不提交真实口令 |
| `IMAP_MAILBOX` | 拉取回复的信箱**文件夹**；入站回复行 `mailbox` 列的取值来源 | `INBOX` | 否 | |
| `IMAP_ADDRESS` | 被拉取信箱**地址**（不是文件夹），拿去与 `REPLY_TO_ADDRESS` 比对 | 空串 | **必填**：启动校验拒绝空值 | 判据 1：须与 `REPLY_TO_ADDRESS` 相同且非空，否则拒绝启动 |
| `IMAP_STARTTLS` | 入站拉取是否启用 STARTTLS | `true` | 否 | `.env.example` 示例填 `false`，与本地 3143 明文口配对；置 `true` 走「明文连上再升级」 |
| `POLL_REVIEW_OUTBOUND_SECONDS` | 流① 安全审查出站邮件的轮询间隔（秒） | `10` | 否 | 间隔必须是配置项，不得硬编码（`AGENTS.md` §8） |
| `POLL_SEND_SECONDS` | 流② 发送邮件的轮询间隔（秒） | `10` | 否 | 同上 |
| `POLL_FETCH_INBOUND_SECONDS` | 流③ 拉取入站回复的轮询间隔（秒） | `10` | 否 | 同上 |
| `POLL_REVIEW_INBOUND_SECONDS` | 流④ 安全审查回复的轮询间隔（秒） | `10` | 否 | 同上 |
| `POLL_CLEANUP_SECONDS` | 流⑤ 保留期清理的执行间隔（秒） | `3600` | 否 | 清理不是时效敏感动作，默认按小时跑 |
| `SMTP_TIMEOUT_SECONDS` | 单次 SMTP 投递的墙钟上界（秒） | `10` | 否 | 超时按「抛异常」走既定重试语义 |
| `IMAP_TIMEOUT_SECONDS` | 单次 IMAP 拉取的墙钟上界（秒） | `10` | 否 | 同上 |
| `REVIEW_TIMEOUT_SECONDS` | 单次审查调用的墙钟上界（秒） | `10` | 否 | 同上 |
| `BATCH_SIZE` | 每批条数，五条流共用；流③ 也用它限制单个 tick 拉取的封数 | `100` | 否 | |
| `MAX_SUBJECT_CHARS` | 主题长度上限（**字符**） | `500` | 否 | API 层校验，超限返回 422 且不落库 |
| `MAX_BODY_BYTES` | 正文长度上限（**字节**） | `1048576` | 否 | 同上（1048576 = 1 MiB，单位写进字段名） |
| `MAX_RECIPIENT_CHARS` | 入站回复寄件人地址（外部可控的 `From`）入库前的**字符**上限 | `320` | 否 | 流③ 超长截断后再入库；不参与 API 校验、不产生 422 |
| `REPLIES_PAGE_SIZE` | `GET /v1/messages/{id}/replies` 每页条数上限（keyset 游标分页） | `100` | 否 | |
| `VS_EMAIL_FORWARDING_IMAGE` | 服务器上要运行的镜像 tag，便于回滚 | `vs-email-forwarding:0.1.0`（示例值） | 否（留空退回 compose 的默认 tag） | **部署专用**：只有服务器上的 `.env` 需要关心；`config.py` 不读 |

## 不进 `.env` 的固定语义

归属判据（`AGENTS.md` §7）：「**改了它等于改产品行为**」的值进 `../src/vs_email_forwarding/constants.py`，
**不得**为它新增 `.env` 项；「**随环境合法地不同**」的值才进 `.env`、只经 `config.py` 读。
`constants.py` 是共享叶子层——不 import 任何项目内模块，各层（含 `db/`）都可以 import 它。
落点清单（取值本身见该文件与各处真源，这里不复述）：

- 状态取值：出站六态与入站终态 `received`，库内与对外同一套、无映射层（语义真源 `../specs/0001-email-forwarding-service/design.md` §2.1 / §2.3）。
- 重试语义：最大尝试次数与退避表——硬约束，出处 `AGENTS.md` §8 与 `docs/research/02-runtime-and-scheduling.md`。
- 受控枚举：正文格式、行方向、SMTP 失败分类、拒审来源。
- 丢弃原因：入站分拣的受控原因集合（`design.md` §3.4）。
- 保留期与时间戳格式（`NFR-8` / `NFR-7`）：保留期是产品语义，可配置的只有清理**间隔**（`POLL_CLEANUP_SECONDS`）。

## 启动即校验三判据

判据在 `config.py` 的 `validate_startup()`，调用点在 `main.py` 的 lifespan 显式启动步骤（不在 import 期，
见 `design.md` §4.4）。三条一次性全部检查后统一报出，**任一违规即拒绝启动、不监听端口**（fail-fast）：

1. `IMAP_ADDRESS` 与 `REPLY_TO_ADDRESS` 相等且非空——不一致时回复回路是静默断掉的；
2. `MAIL_FROM_ADDRESS` 能解析出邮件域——出站 `Message-ID` 的域取自它；
3. `REPLY_TO_ADDRESS` 本身是合法地址（含 `@`、无空白）。

校验信息里只出现**配置项名**，不出现地址本身（`NFR-5`）。

## 集成测试的连接串口径

集成测试**不需要预先建库**（`tests/integration/conftest.py`）：连接串显式设置
`VS_EMAIL_FORWARDING_TEST_DATABASE_URL` 时优先用它，否则退回配置层的 `DATABASE_URL`——两种来源都
**只当服务器坐标**（host / port / 用户 / 口令）使用，库名一律由夹具生成为本次运行的隔离库
（`vs_email_forwarding_test_<进程号>_<8 位十六进制>`），建库后跑 `alembic upgrade head` 建表，
运行结束 `DROP DATABASE ... WITH (FORCE)`；配置里的库名（含开发库）从头到尾不被连接，
仓库里的配置文件一个字都不改。
