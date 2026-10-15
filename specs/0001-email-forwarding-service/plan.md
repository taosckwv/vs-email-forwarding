# 内部邮件中转服务 实现计划

| 项 | 值 |
|---|---|
| spec 编号 | `0001` |
| 对应规格 | `specs/0001-email-forwarding-service/spec.md` |
| 对应设计 | `specs/0001-email-forwarding-service/design.md` |
| 状态 | `已确认` |

## 1. 技术方案概述

**一句话**：把这个服务做成「一个 FastAPI 进程 + 五条进程内 asyncio 后台流 + 三张邮件数据表加一张游标表」，调用方只碰 HTTP，SMTP / IMAP 只碰专用回复邮箱。

**分层怎么落**（依赖方向见 `docs/architecture.md`，目录职责见 `AGENTS.md` §4）：`api/` 只做校验与序列化，`service/` 编排同步请求路径，`flows/` 编排五条后台流，两者并列、互不调用；共用的纯逻辑（`Message-ID` 生成、状态判定、退避表、入站分拣判定）进 `core/`，共用的落库进 `db/` 仓储层，共用的外部能力进 `providers/{smtp,imap,review}/`，配置只有一个入口 `config.py`。

**并发与幂等的骨架**：五条流各自独立循环、独立间隔、独立分批，都走同一套三阶段——**认领（短事务）→ 调用（事务外）→ 落库（第二个事务）**，数据库事务绝不跨越网络调用。认领一律 `SELECT ... FOR UPDATE SKIP LOCKED` 配 `attempt_count` / `next_attempt_at`，状态写入一律带谓词的条件 UPDATE，`rowcount = 0` 按幂等忽略。失败重试首试 + 3 次重试（合计 4 次尝试）、退避 15s / 30s / 60s；认领谓词放宽到 `attempt_count <= 4`，第 4 次认领后崩溃的遗留行由**收割支**直接推进终态（不重跑、不产生第 5 次调用）。细节见 `design.md` §3，本计划 §5 只写实现口径，不复述论证。

**数据落点**：出站邮件与入站回复共用一张 `messages` 表（`direction` 区分，回复用自引用外键指向出站行、级联删除）；等待审查的行放 `pending_messages`（纯队列，过审即删行）；审查未通过或审查环节出错的行放 `rejected_messages`；IMAP 进度放 `imap_cursors`。全库 4 张表，字段、索引、`CHECK` 与状态机见 `design.md` §2。

**对外契约**：三个端点——`POST /v1/messages` 提交、`GET /v1/messages/{id}` 查状态、`GET /v1/messages/{id}/replies` 查回复（keyset 游标分页），外加既有的 `GET /healthz`。**接口契约（字段、类型、取值约束）的唯一真源是 `docs/api-contract.yaml`（OpenAPI 3.1），本次新增**。状态六种（已提交 / 审查中 / 拒审 / 投递中 / 已发出 / 发送失败），**库内与对外同一套取值、不存在映射层**：「投递中」（`sending`）就是 `messages.status` 的列值。见 `design.md` §2.1 落点表与 §4.1。

**一期是空实现与无鉴权**：审查恒返回 `pass`（必须显式标注，`NFR-6`），API 层只在 router 上挂一个当前为空的鉴权依赖占位（`F3`）。

**被否掉的整体做法**（只列一句，论证在 `design.md` §6）：**单事务到底**——认领、审查 / 发信调用、写结果放同一个事务，会让数据库事务跨越一次可达数十秒的 SMTP 调用，同批行全被锁住，且未提交的中间态外部不可观测、「审查中」无法兑现；**引入中间态**（`sending` / `retrying`）——崩溃后卡在中间态的行需要额外的超时回收机制，而恢复口径本来就已经是「状态字段 + `next_attempt_at`」；**引入 broker**（Celery / APScheduler / cron）——本项目是固定间隔的纯数据库轮询，进程内 asyncio 已够，且单次 tick 是个普通 async 函数、测试 `await` 一次即可，不必等 10 秒。

## 2. 涉及文件与目录

按「新增 / 修改」列到**文件**一级，并按落点分组；每个文件只写一行职责。分组的依据是 `AGENTS.md` §4 的目录职责表——**分层归错层是本节要拦住的主要返工原因**，因此每个分组都把「不放什么」写在标题里。

依赖方向以 `docs/architecture.md` 为唯一出处。除既有分层外，本次新增一个**共享叶子层** `src/vs_email_forwarding/constants.py`（只放服务固定语义常量，不 import 任何项目内模块，各层含 `db/` 都可 import），并已就地更新 `AGENTS.md` §4 / §7 与 `docs/architecture.md`。

### 2.0 `constants.py`（服务固定语义常量；共享叶子层，各层可 import）

| 文件 | 动作 | 说明 |
|---|---|---|
| `src/vs_email_forwarding/constants.py` | `已落地（本轮只做核对）` | 六态取值与中文名、合法迁移、`messages.status` 的 `CHECK` 取值集合、十一类丢弃原因、拒审来源、`ErrorClass`、`MAX_ATTEMPTS = 4` 与退避表 `{1: 15, 2: 30, 3: 60}`、`4xx`/`5xx` 区间、时间戳格式。**这批取值已在前几轮提交里落地**（依据 `docs/README.md` §4.9「文档与代码不一致时以代码为准」），本轮只做与 `design.md` §2.3 / §3.4 的逐项核对（`T-1`）。**只放取值的文本，不放判定逻辑**；不得 import 任何项目内模块 |

**为什么它必须独立成层**：这些取值同时被 `db/`（`CHECK` 常量与列值）、`core/`（判定与退避）、`schemas/`（响应枚举）、`flows/`（计数与分类）用到，而 `docs/architecture.md` §2 的依赖表把 `db/` 写成「无——只依赖 ORM 与会话本身」。若把它们放进 `core/`，`db/` 就得反向依赖 `core/`，直接违规；放进 `config.py` 则会把「服务固定语义」与「随环境变化的配置」混成一个模块，并诱发把重试次数加进 `.env`（那会破坏 `AGENTS.md` §8.7）。因此改为**新增一层零依赖的叶子模块**，并在架构文档中声明它是唯一例外。

**与 `.env` 的分界**（`AGENTS.md` §7 新增条款）：判据是「**改了它等于改产品行为**」则进 `constants.py`；「**随环境合法地不同**」则进 `.env`、只经 `config.py` 读。因此 `MAX_SUBJECT_CHARS`、`MAX_BODY_BYTES`、`BATCH_SIZE`、`REPLIES_PAGE_SIZE`、六个 `POLL_*`、三个超时**仍然留在 `config.py`**，不搬进常量模块。

### 2.1 `core/`（纯逻辑，无 I/O、无数据库）

| 文件 | 动作 | 说明 |
|---|---|---|
| `src/vs_email_forwarding/core/message_id.py` | `新增` | 出站 `Message-ID` 的生成与解析：形如 `<{UUID}@{域}>`，域取自 `MAIL_FROM_ADDRESS`（带显示名时先取 `<...>` 内的地址，`design.md` §3.1） |
| `src/vs_email_forwarding/core/retry.py` | `新增` | 重试算术：`next_attempt_at(now, attempt) -> datetime`（**`now` 由调用方传入**，本模块不自行取时、不碰时钟），并据 `SMTP_TRANSIENT_*` / `SMTP_PERMANENT_*` 区间把返回码分类（**取值来自 `constants.py`，本模块只写算术**） |
| `src/vs_email_forwarding/core/states.py` | `新增` | **唯一的六态判定函数**：按 `design.md` §4.1 的优先级自上而下判定，并给出每个状态的物理落点；库内与对外共用同一套取值，**没有状态映射层** |
| `src/vs_email_forwarding/core/inbound.py` | `新增` | 入站纯判定：DSN 结构判定（`multipart/report` + `message/delivery-status`）、MDN 判定、`In-Reply-To` / `References` 解析与关联匹配；丢弃原因从 `constants.DISCARD_REASONS` 取（`design.md` §3.4） |

### 2.2 `db/`（ORM、引擎与会话、仓储层；所有 SQL 只在这里，不放业务规则）

本层的第三方库是**新增的运行时依赖**（`design.md` §5 的选型，已在 `pyproject.toml` 里）：`SQLAlchemy` 管 ORM / 引擎 / 会话 / 查询构造，`asyncpg` 作异步驱动；`Alembic` 只在 `migrations/` 用，不在本层运行时路径上。**「SQL 只在仓储层」约束的是「不许出现在别的层」，不是「这一层要写裸 SQL 字符串」**——写法口径见 §2.9。

| 文件 | 动作 | 说明 |
|---|---|---|
| `src/vs_email_forwarding/db/models.py` | `新增` | 四张表的 ORM 模型：字段、受控枚举的 `CHECK`、索引、`parent_id` 自引用外键与级联删除（`design.md` §2） |
| `src/vs_email_forwarding/db/session.py` | `新增` | 进程级异步引擎单例、会话工厂、`dispose()`；短会话按阶段开、用完即关（`docs/architecture.md` §5） |
| `src/vs_email_forwarding/db/repositories/pending.py` | `新增` | 待审表的 SQL：出站建行 `insert_outbound_pending`（流① 认领前的「提交即入队」）、三阶段认领（`FOR UPDATE SKIP LOCKED` + `attempt_count <= 4`）、过审 / 拒审落库与删行、`pending_messages` 到 `rejected_messages` 的收割支写入 |
| `src/vs_email_forwarding/db/repositories/messages.py` | `新增` | 邮件表的 SQL：发信认领与条件 UPDATE、退信置失败（仅 `sent_at` 非空）、回复行的 keyset 分页查询、出站行的六态判定查询 |
| `src/vs_email_forwarding/db/repositories/rejected.py` | `新增` | 拒审表的 SQL：写入与按 ID 查询存在性 |
| `src/vs_email_forwarding/db/repositories/cursors.py` | `新增` | 游标表的 SQL：读取 / 初始化（`UIDNEXT - 1`）/ `UIDVALIDITY` 变化时的重置、游标与单封落库的同事务推进 |
| `src/vs_email_forwarding/db/repositories/cleanup.py` | `新增` | 保留期清理的 SQL：按 `created_at` 分批 `FOR UPDATE SKIP LOCKED` 删除，出站行用 `NOT EXISTS` 排除仍有入站待审行的行 |
| `src/vs_email_forwarding/db/repositories/inbound.py` | `新增` | 两处入站唯一键（`(mailbox, uidvalidity, uid)` 部分唯一索引，`WHERE direction = 'inbound'`）的幂等 SQL：流③ `insert_inbound_pending` 写待审行、流④ `approve_inbound_pending` 同事务写 `received` 行并删待审行（`created_at` 原样搬运），两处均以 `ON CONFLICT DO NOTHING` 携带同一谓词忽略重复（`AC-11`；`design.md` §2.2 / §3.4 / §3.5） |
| `src/vs_email_forwarding/db/repositories/__init__.py` | `新增` | 仓储包的占位职责文件（包目录约定，`AGENTS.md` §4） |
| `src/vs_email_forwarding/db/__init__.py` | `修改` | 现文档说「表结构尚未定案、暂无实现」——表结构已在 `design.md` §2 定案，改为指向该节与新模块（**文档性修改**，无 I/O 行为） |

### 2.3 `providers/`（外部能力适配器；各自 `base.py` 定接口，不放业务状态流转）

| 文件 | 动作 | 说明 |
|---|---|---|
| `src/vs_email_forwarding/providers/review/base.py` | `新增` | 审查接口：中性输入对象（`direction` / `message_id` / 内容四字段）+ `pass` / `reject` 受控枚举（`design.md` §4.2） |
| `src/vs_email_forwarding/providers/review/always_pass.py` | `新增` | 空实现，恒返回 `pass`；**必须显式标注「空实现、永远通过」**（`NFR-6` / `AC-25`） |
| `src/vs_email_forwarding/providers/review/factory.py` | `新增` | 按配置选取实现的工厂函数；一期恒为空实现，**不提供关掉审查流的运行时配置** |
| `src/vs_email_forwarding/providers/smtp/base.py` | `新增` | 发信接口：接收中性发信对象、返回受控判定；`From` 与 `Reply-To` 由适配器显式写入（`design.md` §4.3） |
| `src/vs_email_forwarding/providers/smtp/smtplib_sender.py` | `新增` | 标准库 `smtplib` 实现，经 `asyncio.to_thread` 调用；受 `SMTP_TIMEOUT_SECONDS` 约束；只回传 SMTP 返回码与永久 / 临时分类，**不落任何自由文本** |
| `src/vs_email_forwarding/providers/imap/base.py` | `新增` | 拉取接口：把邮件取回并解析成中性结构（头字段、MIME 部件、`mailbox` / `uidvalidity` / `uid` 三要素），**不做任何判定** |
| `src/vs_email_forwarding/providers/imap/imapclient_fetcher.py` | `新增` | `IMAPClient` 实现，经 `asyncio.to_thread` 调用；受 `IMAP_TIMEOUT_SECONDS` 约束；含 `UIDVALIDITY` / `UIDNEXT` 与逐封取回 |

### 2.4 `flows/`（定时任务的入口与编排；不放 HTTP 路由与 SQL 字符串）

| 文件 | 动作 | 说明 |
|---|---|---|
| `src/vs_email_forwarding/flows/scheduler.py` | `新增` | 五条后台任务的启停与调度：各自独立循环、独立间隔、各自吞异常；由 `main.py` 的 lifespan 驱动 |
| `src/vs_email_forwarding/flows/review_outbound.py` | `新增` | 流① 审查出站：认领 → 调审查 → 落库（含收割支） |
| `src/vs_email_forwarding/flows/send_email.py` | `新增` | 流② 发信：认领 → SMTP 投递 → 条件 UPDATE（含收割支） |
| `src/vs_email_forwarding/flows/fetch_inbound.py` | `新增` | 流③ 拉取入站：取回 → 分拣（回复 / 退信 / 回执 / 丢弃）→ 落库、置失败或按十一类计数；游标逐封同事务推进 |
| `src/vs_email_forwarding/flows/review_inbound.py` | `新增` | 流④ 审查回复：认领 → 调审查 → 写 `received` 或将主题与正文写入拒审表（含收割支） |
| `src/vs_email_forwarding/flows/cleanup.py` | `新增` | 流⑤ 保留期清理：分批删除，父行排除仍有入站待审行的出站行，回复由外键级联清除 |

### 2.5 `service/` 与 `schemas/`（同步请求路径的编排与请求 / 响应模型）

| 文件 | 动作 | 说明 |
|---|---|---|
| `src/vs_email_forwarding/schemas/messages.py` | `新增` | Pydantic 请求 / 响应模型：提交请求、状态响应、回复分页响应（含 `found`、`next_cursor`）；时间字段序列化为 UTC RFC 3339 六位小数 + `Z` |
| `src/vs_email_forwarding/service/submit_message.py` | `新增` | 用例：校验通过后生成 `Message-ID` 并写一行待审行，返回 `{id, status: submitted}` |
| `src/vs_email_forwarding/service/query_message.py` | `新增` | 用例：单个事务快照内按 `design.md` §4.1 的判定表自上而下判定六态，并做 ORM → Pydantic 的转换（`design.md` §8） |
| `src/vs_email_forwarding/service/query_replies.py` | `新增` | 用例：按 `received_at ASC, id ASC` 做 keyset 翻页，返回回复集合与 `next_cursor` |

### 2.6 `api/`（HTTP 接口层；不碰仓储、不直连 SMTP / IMAP、签名里不出现 SQLAlchemy 类型）

| 文件 | 动作 | 说明 |
|---|---|---|
| `src/vs_email_forwarding/api/v1/messages.py` | `新增` | 三个端点的路由与请求校验：`POST /v1/messages`、`GET /v1/messages/{id}`、`GET /v1/messages/{id}/replies`；422 清单与未知字段忽略策略见 `design.md` §4.1 |
| `src/vs_email_forwarding/api/v1/dependencies.py` | `新增` | **当前为空的鉴权依赖占位**：挂在 router 上，后续替换成真实校验即可、不重排路由结构（`F3`） |
| `src/vs_email_forwarding/api/v1/__init__.py` | `修改` | 注册 `messages` 路由，并把鉴权占位依赖挂到 `api_router` 上 |
| `src/vs_email_forwarding/api/v1/health.py` | `修改` | `/healthz` 补**一次轻量数据库探测**；语义仍是「进程可用」，**不表达五条流是否在跑**（`design.md` §4.1） |

### 2.7 `main.py` 与配置层（进程组装与唯一配置入口）

| 文件 | 动作 | 说明 |
|---|---|---|
| `src/vs_email_forwarding/config.py` | `修改` | 新增 `validate_startup()`：三条判据（`IMAP_ADDRESS` 等于 `REPLY_TO_ADDRESS`、`MAIL_FROM_ADDRESS` 能解析出邮件域、`REPLY_TO_ADDRESS` 合法）；校验信息只出现**配置项名**，不出现地址本身（`design.md` §4.4） |
| `src/vs_email_forwarding/main.py` | `修改` | lifespan 显式启动步骤里调用 `validate_startup()`（**不放在 import 期**），再创建引擎与启动五条后台任务；关闭阶段取消任务并 `dispose()` |

### 2.8 仓库其他位置

| 文件 | 动作 | 说明 |
|---|---|---|
| `migrations/alembic.ini` | `新增` | Alembic 配置：不写死连接串（URL 从 `config.py` 读）、`script_location = migrations` |
| `migrations/env.py` | `新增` | 异步迁移外壳：用 SQLAlchemy 异步引擎跑迁移，`target_metadata` 指向 `db/models.py`（`design.md` §5） |
| `migrations/script.py.mako` | `新增` | 迁移脚本模板（`alembic init` 产物，按需中文化注释） |
| `migrations/versions/0001_create_mail_tables.py` | `新增` | 首版迁移：四张表、六项索引、全部 `CHECK`；`upgrade()` / `downgrade()` 都要实现（§3.2） |
| `.env.example` | `修改` | 与前一轮已落地的 9 个新配置项对齐（内容项本身已在文件里，本次只做一致性复核） |
| `migrations/README.md` | `修改` | 「约定」第 3 条文件名**就地改写**：删掉「保留 Alembic 自动生成的 revision 哈希前缀」，改为序号前缀 + 下划线（例 `0001_create_mail_tables.py`）；并同步「当前状态」段（初始化已落地） |
| `docs/api-contract.yaml` | `新增` | **对外 HTTP 接口契约的唯一真源**（OpenAPI 3.1）：三个端点的路径、请求 / 响应字段、类型、必填、取值枚举与取值约束（长度上限、地址格式、UUID 格式）、错误响应。**字段定义只此一份**，`design.md` §4.1 只留设计理由 |
| `docs/configuration.md` | `新增` | 配置项清单文档：逐项写用途、默认值、是否必填、启动即校验的三条判据；`docs/README.md` §3 的「尚未创建的文档」清单要**同时移除它** |
| `docs/README.md` | `修改` | ① §2 导航表加两行「改配置项 → `configuration.md`」「查接口契约 → `api-contract.yaml`」；② §3 的「尚未创建的文档」清单移除 `configuration.md` |
| `docs/research/01-delivery-and-deploy.md` | `修改` | 按本轮裁决记两件事：**D2-甲** 三件脚本的落地位置与判据、**D2-乙 随 D13 推迟**；「落地位置」段的「待实现」列表相应收窄 |
| `docs/open-questions.md` | `修改` | D2 就地**收窄**为「剩余：产出方那侧脚本（构建 / tar 导出 / load / 构建输入传输），随 D13 推迟」；已定的 D2-甲 从该条移除 |
| `deploy/migrate.sh` | `新增` | 迁移入口（**唯一入口**）：容器内 `docker compose run --rm app uv run alembic -c migrations/alembic.ini upgrade head`，不加 `--no-deps`、不用 `exec`，并打印「无版本时的失败属预期」这条判别法。**LF、无 BOM**（`AGENTS.md` §7） |
| `deploy/run.sh` | `新增` | 起服务 + 冒烟：`docker compose up -d` 后轮询 `/healthz`（默认 30 次 × 2s，可用环境变量覆盖）；从 `.env` 解析端口要容忍 CR、引号与行尾注释；宿主无 `curl` 时降级为容器内 Python 标准库探测。**`compose.yaml` 的注释已经引用了这个名字** |
| `deploy/check_archive.py` | `新增` | 交付前单平台归档自检：**不依赖 Docker、只用 Python 标准库**；支持 OCI（`index.json`）与 legacy docker-archive（`manifest.json`）两种布局；显式指定期望基础镜像个数（新链路 = 3）；退出码固定 `0` / `1` / `2`；**必须在生成 `.sha256` 旁路文件之前执行** |
| `deploy/README.md` | `修改` | 从纯职责说明改为脚本清单：三件脚本各自的用途、入口、判据出处，并写明 D2-乙 那侧脚本**本期不在范围**、随 D13 推迟 |
| `tests/unit/test_message_id.py` | `新增` | 纯逻辑：`Message-ID` 生成与带显示名时取域的解析（随 `T-13` 先红，§7.3） |
| `tests/unit/test_retry_policy.py` | `新增` | 纯逻辑：退避表、`4xx` / `5xx` 分类、终态判据，以及 `AC-21` 的最坏路径算术（随 `T-14` 先红，§7.3） |
| `tests/unit/test_states.py` | `新增` | 纯逻辑：六态判定的优先级与库内落点（随 `T-15` 先红，§7.3） |
| `tests/unit/test_inbound_sorting.py` | `新增` | 纯逻辑：DSN / MDN 结构判定、`References` 解析与关联匹配、十一类丢弃原因（随 `T-16` 先红，§7.3） |
| `tests/unit/test_scheduler_intervals.py` | `新增` | `AC-23` 的确定性判定：调度器构造时读到的间隔 == 配置值（改配置后重建即为新值），并静态断言间隔与批量不以字面量出现在源码中（随 `T-22` 先红，§7.3） |
| `tests/unit/test_request_validation.py` | `新增` | 纯逻辑：请求校验（多收件人、非法地址、CR/LF 头注入、超限、非法 `body_format`）（随 `T-30` 先红，§7.3） |
| `tests/unit/test_settings_validation.py` | `新增` | 纯逻辑：`validate_startup()` 三条判据各自制造一次违规（随 `T-32` 先红，§7.3） |
| `tests/unit/test_openapi_contract.py` | `新增` | 契约一致性：把 FastAPI 应用生成的 OpenAPI（`app.openapi()`）与 `docs/api-contract.yaml` 逐字段比对（路径、字段名、必填、`enum`），防两处漂移（随 `T-38`；**留阶段 5**——三方产物齐备后才写得出） |
| `tests/unit/test_constants_consistency.py` | `新增` | 三方取值一致：`docs/api-contract.yaml` 的 `MessageState` / `BodyFormat` 枚举 == `constants.py` 的取值集合 == 迁移与 `db/models.py` 中 `CHECK` 的取值集合；另断言 `constants.py` **零项目内 import**（共享叶子层的硬约束，靠静态读源码判定）（随 `T-38`；**留阶段 5**） |
| `tests/unit/test_repository_statements.py` | `新增` | 抢占写法唯一（`AC-26`）：静态 AST 扫描认领函数、断言不存在「先 `SELECT` 判断再 `UPDATE`」的两步写法，并断言认领语句编译出的 SQL 同时含 `FOR UPDATE` 与 `SKIP LOCKED`（随 `T-6` 先红，§7.1 / §7.3） |
| `tests/unit/test_config_hygiene.py` | `新增` | 配置口径（`AC-27`）：除 `config.py` 外没有模块直接读环境变量、没有第二处读 `.env`；`git ls-files` 含 `.env.example` 且不含 `.env`（随 `T-32` 先红） |
| `tests/unit/test_review_placeholder.py` | `新增` | 空实现标注（`AC-25`）：`providers/review/{base,always_pass}.py` 开头注释含「空实现」「永远通过」，`docs/security.md` 安全边界一节关键词可检索（随 `T-17` 先红） |
| `tests/unit/test_schemas.py` | `新增` | 响应模型：字段集合与契约一致、时间序列化为 UTC RFC 3339 六位小数 + `Z`（`NFR-7` / `AC-17`）（随 `T-27` 先红） |
| `tests/integration/conftest.py` | `新增` | 集成夹具：真实 PostgreSQL（测试库 `vs_email_forwarding_test`）、假 SMTP / 假 IMAP、可替换的假审查实现、测试库清表（`T-33`，§7.2） |
| `tests/integration/test_review_outbound_flow.py` | `新增` | 流① 集成用例（`T-34`，§7.3） |
| `tests/integration/test_send_flow.py` | `新增` | 流② 集成用例，含 `AC-4` / `AC-5` / `AC-7` / `AC-8` / `AC-9` / `AC-19` / `AC-21` / `AC-24` / `AC-29` / `AC-30` / `AC-34` / `AC-37` / `AC-40`（`T-35`） |
| `tests/integration/test_fetch_inbound_flow.py` | `新增` | 流③ 集成用例，含 `AC-10` ~ `AC-14` / `AC-28` / `AC-38` / `AC-39`（`T-36`） |
| `tests/integration/test_review_inbound_flow.py` | `新增` | 流④ 集成用例（`T-34`） |
| `tests/integration/test_cleanup_flow.py` | `新增` | 流⑤ 集成用例（`AC-22`）（`T-37`） |
| `tests/integration/test_api_contract.py` | `新增` | 三个端点的 HTTP 契约，含 `AC-1` ~ `AC-5` / `AC-15` ~ `AC-18` / `AC-31` / `AC-33` / `AC-35` / `AC-36`（`T-38`） |
| `tests/integration/test_repositories.py` | `新增` | 仓储层并发与幂等：`FOR UPDATE SKIP LOCKED`、条件 UPDATE、收割支、清理的 `NOT EXISTS`、keyset 翻页（`AC-26` / `AC-29` / `AC-41`）（`T-37`） |
| `tests/integration/test_startup_validation.py` | `新增` | 三条启动校验各自制造一次违规，断言进程非零退出且不监听端口（`AC-32`）（`T-38`） |
| `tests/fixtures/` | `新增` | 静态邮件样本：DSN、MDN、带 `In-Reply-To` / `References` 的回复、原邮件、无法关联的样本；**只用 `example.com` 占位地址**。放数据不放可执行测试代码（§7.2） |
| `tests/integration/README.md` | `修改` | 「覆盖重点」第 1 条仍是旧词「待审 → 已排队 → 已发送 → 已收到 / 拒审 / 终态失败」，按六态取值就地改写；补上夹具落点（`tests/integration/conftest.py`）与测试库准备命令 |
| `tests/fixtures/README.md` | `修改` | 从「当前阶段为空」改为实际内容清单（样本落点与命名），保留「不出现真实地址与凭据」的约定 |
| `README.md` | `修改` | 「当前状态」段与「生产部署（现状）」段同步到实现后的现状；目录结构块补 `constants.py` 一行（§6.2） |
| `CHANGELOG.md` | `修改` | 实现完成后补一条版本条目（提交信息按 Conventional Commits + 中文，`AGENTS.md` §9） |

### 2.9 仓储层的写法口径与迁移 DDL 的边界

**仓储层一律用 SQLAlchemy 表达式构造器，全层不出现裸 SQL 字符串**（即不使用 `text("SELECT …")`）。这是**本层唯一允许的写法**，不是风格偏好：`AGENTS.md` §8 的抢占硬约束要求认领语句**同时含 `FOR UPDATE SKIP LOCKED`**，用构造器写出来才具备「不连数据库也能断言生成的 SQL」这一可测性（`AC-26`）。

四种语句形态已在本机虚拟环境（SQLAlchemy 2.1.4 / asyncpg 0.32.0）**逐个编译验证**：

| 用途 | 构造器写法 | 编译产物（关键片段） |
|---|---|---|
| 认领（硬约束） | `select(...).with_for_update(skip_locked=True)` | `… ORDER BY next_attempt_at LIMIT %(param_1)s FOR UPDATE SKIP LOCKED` |
| 状态写入（条件 UPDATE） | `update(...).where(<声明的起点>).values(...).returning(...)` | `UPDATE messages SET status=%(status)s WHERE id = %(id_1)s AND status = %(status_1)s RETURNING id`（起点即 `sending`，见 §5.2） |
| 幂等插入（撞唯一键按幂等忽略） | `pg_insert(...).on_conflict_do_nothing(index_elements=[...])` | `INSERT … ON CONFLICT (id) DO NOTHING` |
| 清理父行的 `NOT EXISTS` | `delete(...).where(..., ~exists(select(...)))` | `DELETE FROM messages WHERE … AND NOT (EXISTS (SELECT …))` |

`rowcount = 0` 与撞唯一键 / 主键的处理都据此表达，不依赖异常文本匹配。

- **Alembic 迁移文件是唯一例外**：那里只能用 `op.create_table()` / `op.create_index()` 这类命令式 DDL 接口（本身就是 DDL 构造器，不是运行时查询语句），与仓储层写法不冲突。
- **兜底纪律**：若某条语句的 PostgreSQL 方言特性确实无法用构造器表达，**当场停下来问所有者**——不得改用 `text()` 裸串绕过，那会让 `AC-26` 的静态检查失去意义，也会引入拼接注入面。**裁决的落点固定**：若裁决改变了仓储层的写法口径，先按 `AGENTS.md` §6 改写 `design.md` §5 与本节，再继续实现；裁决未落定之前，该任务不算完成。

### 2.10 包职责说明中已过时的占位文字（文档性修改）

这些文件的 `__init__.py` 里写着「**尚未定案**」，而对应的结论已经在 `design.md` 或 `docs/api-contract.yaml` 里定案了。按 `docs/README.md` §4「文档与代码不一致时以代码为准，并提一个修正文档的改动」，本次实施顺带把它们改成指向当前真源——**纯 docstring 修改，无行为变化**，但漏改会让后来者按「未定案」去 `open-questions.md` 找一个已经不在那里的条目。

| 文件 | 动作 | 现在写着（过时） | 改成指向 |
|---|---|---|---|
| `src/vs_email_forwarding/core/__init__.py` | `修改` | 状态机「pending / queued / sent / received / rejected / failed 等」 | 六态统一取值（`design.md` §2.3）+ 本目录新增的四个模块 |
| `src/vs_email_forwarding/db/__init__.py` | `修改` | 「表结构尚未定案，Alembic 迁移方案同样未定」 | `design.md` §2 + `db/models.py`、`db/repositories/` |
| `src/vs_email_forwarding/schemas/__init__.py` | `修改` | 「字段与状态枚举取值尚未定案」 | `docs/api-contract.yaml` |
| `src/vs_email_forwarding/api/v1/__init__.py` | `修改` | 「投递与回复查询路由的契约字段尚未定案」 | `docs/api-contract.yaml`；并补上鉴权占位依赖的挂载位置 |
| `src/vs_email_forwarding/providers/smtp/__init__.py` | `修改` | 「客户端库选型与接口协议尚未定案」 | 标准库 `smtplib` + `providers/smtp/base.py`（`design.md` §5） |
| `src/vs_email_forwarding/providers/imap/__init__.py` | `修改` | 「客户端库选型与接口协议尚未定案」 | `IMAPClient` + `providers/imap/base.py`（`design.md` §5） |
| `src/vs_email_forwarding/providers/review/__init__.py` | `修改` | 空实现待定、接口协议不定案；空实现文件名写作 `noop.py` | `providers/review/{base,always_pass,factory}.py`（`design.md` §4.2）；保留「一期空实现、真实审查能力何时补上仍未定」这一半（那是 `open-questions.md` 的未定项，**不要写成结论**） |

## 3. 数据模型变更

**本节的表结构真源是 `design.md` §2，本节不重抄字段清单**——只写「本次要落成什么、落在哪个迁移文件、有没有回填」。任何字段、索引与 `CHECK` 的改动都必须先按 `AGENTS.md` §6 改写 `design.md` §2，再回到本节调整。

### 3.1 四张表、六项索引与约束的落点

| 表（物理名 / 中文名） | 动作 | 内容 |
|---|---|---|
| `pending_messages` / 待审表 | `新增` | 在审邮件的唯一落点，出站与入站共用、`direction` 区分；**不设 `status` 列**，状态由「这一行在哪张表里」承担 |
| `messages` / 邮件表 | `新增` | 单表承载出站行与入站回复行；`parent_id` 自引用外键 + 级联删除（`NFR-8` 的「邮件与它的回复一起移除」靠它，不靠应用层写两条删除） |
| `rejected_messages` / 拒审表 | `新增` | 审查判定不通过或审查环节出错的行；出站拒审**不保留原文**，入站拒审保留主题与正文供复核 |
| `imap_cursors` / 游标表 | `新增` | 每个信箱文件夹一行；`UIDVALIDITY` 一变就重置 `last_uid` 这条规则在表结构上一眼可见 |

**索引与约束逐项对应到需求**（名字与判据见 `design.md` §2.2）：

| 项 | 落点 | 它兜住哪一条 |
|---|---|---|
| `message_id` 全局唯一 | `messages` | 出站行必填、入站关联的解析依据 |
| `(mailbox, uidvalidity, uid)` 部分唯一 `WHERE direction = 'inbound'` | `pending_messages` **与** `messages` 各一份 | 入站幂等（`AC-11`）；两处都建，游标回退时在落待审行那一步就被挡住 |
| `(status, next_attempt_at)` | `messages` | 流② 的 `FOR UPDATE SKIP LOCKED` 抢占（§5.1） |
| `(direction, next_attempt_at)` 部分索引 `WHERE attempt_count <= 4` | `pending_messages` | 流① / 流④ 的抢占；**首版即建，不等积压出现** |
| `(parent_id, received_at)` | `messages` | `GET /v1/messages/{id}/replies` 的 keyset 翻页（`AC-41`） |
| `(created_at)` | `messages`、`rejected_messages` | 保留期清理的判据 |
| `CHECK`：受控枚举取值集合（`direction` / `status` / `body_format` / `last_error_class` / `source`） | 三张邮件表 | 受控枚举以文本列存储、由 `CHECK` 兜底，**不用原生 `ENUM`** |
| `CHECK`：`direction` 与 `status` 的合法组合 | `messages` | 入站行只能 `received`，出站行不得为 `received` |
| `CHECK`：行模型的必填与反向禁止 | `messages` | 出站行必须有 `message_id`、不得有 `mailbox` / `uidvalidity` / `uid` / `parent_id`；入站行反过来——堵住混装行的 NULL 语义漏洞 |
| `CHECK`：`subject` / `body` 仅入站行非空 | `rejected_messages` | 拒审表只对入站回复保留可复核内容 |
| `mailbox` 唯一 | `imap_cursors` | 每个信箱文件夹一行 |

### 3.2 迁移文件

- 迁移文件：`migrations/versions/0001_create_mail_tables.py`（`新增`）
- `upgrade()`：按 3.1 建**四张表 + 六项索引 + 全部 `CHECK` + 一处自引用外键**，一次建齐——首版即建那两条部分索引（`pending_messages` 的认领索引、两处入站幂等索引），不留到「积压出现时再补」。
- `downgrade()`：按依赖倒序删四张表（先 `messages` 的自引用外键所在表，再其余），**必须实现**（`migrations/README.md` §约定 2）；首版没有丢数据的不可逆步骤。
- 迁移经由 `deploy/migrate.sh` 以异步外壳执行（`design.md` §5），不在宿主机手工跑 Alembic（`migrations/README.md` §约定 6）。
- 文件名遵循本轮定稿的约定：**序号前缀 + 描述用下划线**，例即 `0001_create_mail_tables.py`（`migrations/README.md` 第 23 条同批改写，见 §2.8）。

### 3.3 数据回填与兼容处理

**无。** 库中此前不存在任何业务表（`src/vs_email_forwarding/db/` 至今只有包占位），本次是纯新增表结构，没有历史数据需要搬运、没有字段改名、没有默认值回填；也不存在「旧版本读新数据」的兼容问题（没有任何旧版本读写过这些表）。

## 4. 接口变更

**完整的请求字段、响应体字段、六态判定表与 422 清单在 `design.md` §4.1，本节不转抄**——只写「新增哪几个端点、契约与状态语义落在哪个文件、有没有兼容性影响」。契约变更必须先按 `AGENTS.md` §6 改写 `design.md` §4.1，再回到本节。

### 4.1 端点与请求 / 响应契约的落点

| 端点 | 变更 | 契约细节 |
|---|---|---|
| `POST /v1/messages` | `新增` | `design.md` §4.1 |
| `GET /v1/messages/{id}` | `新增` | 同上 |
| `GET /v1/messages/{id}/replies` | `新增` | 同上（keyset 游标分页，每页条数上限来自 `REPLIES_PAGE_SIZE`） |
| `GET /healthz` | `修改` | 路径与响应形状不变；按 `design.md` §4.1 补**一次轻量数据库探测**，语义仍是「进程可用」、**不表达五条流是否在跑** |

### 4.2 状态取值与错误处置的落点

**与库内状态的一致性**：库内与对外**是同一套六态取值**（`submitted` / `reviewing` / `rejected` / `sending` / `sent` / `failed`），**没有映射层**——「投递中」（`sending`）就是 `messages.status` 的列值，而出站行的合法迁移只有 `sending → sent` / `sending → failed` / `sent → failed`（`design.md` §2.3）。因此状态列**不需要迁移**（`CHECK` 的取值集合就是这四种列值），读取路径也**不做任何状态翻译**；`submitted` / `reviewing` / `rejected` 的落点由「行在哪张表 + `attempt_count`」决定（`design.md` §2.1 落点表）。

| 要落的东西 | 落在哪 | 说明 |
|---|---|---|
| 六态**判定函数**（自上而下的优先级） | `core/states.py` | 纯逻辑、无 I/O，可单测；对应 `design.md` §4.1 的判定表。**全项目唯一的判定落点**，`service/` 与 `api/` 都调用它，不得各自再实现一遍 |
| 六态取值本身 | `schemas/messages.py` | 响应模型的受控取值；与库内**同一套**名字，不另立一套类型名 |
| 接口契约（字段、类型、必填、取值约束、错误响应） | `docs/api-contract.yaml` | **唯一真源**；响应模型必须与它逐字段对齐 |
| 「ID 不存在」与「存在但尚无回复」的区分 | `schemas/messages.py` + `service/query_message.py` | 靠响应体的 `found` 标志，**不靠 HTTP 状态码、也不靠空集合**（`AC-31` / `AC-35`） |
| 判定在**单个事务快照**内完成 | `service/query_message.py` | 三次查询同一快照，避免读到跨表搬运的中间组合而误报 `found: false` |
| 时间字段格式（UTC RFC 3339、固定六位小数、`Z` 结尾） | `schemas/messages.py` 的序列化 | 与库中 `timestamptz` 一一对应（`NFR-7`、`AC-17`），格式约束同见契约文件 |
| 422 与「未知字段忽略」两类入站处置 | `api/v1/messages.py` | 校验清单见 `design.md` §4.1；未知字段忽略使 `AC-4` 自动成立，不需要专门写忽略逻辑 |
| 鉴权位置占位（当前为空） | `api/v1/dependencies.py` | 挂在 router 上，后续替换成真实校验不重排路由结构（`F3`） |

### 4.3 兼容性影响

**首次交付，无历史契约**：这三个端点此前不存在，没有任何调用方依赖旧行为，因此本次不存在向后兼容问题。`GET /healthz` 路径与响应形状不变，只是内部多一次数据库探测。

**需要留给后续的判断（不在本计划内自行决定）**：新端点一旦被调用方按固定契约实现，后续任何字段增删或**六态语义变更**都是破坏性变更，而一期没有版本协商机制——真要改，属新的决策，要按 `AGENTS.md` §6 改写 `design.md` §4.1。

## 5. 并发与幂等设计

**硬约束**（`AGENTS.md` §8、`docs/research/02-runtime-and-scheduling.md`）：五条后台任务的处理必须幂等；抢占一律 `SELECT ... FOR UPDATE SKIP LOCKED` 配状态字段，外加 `next_attempt_at` 控制退避；**禁止「先 `SELECT` 判断、再 `UPDATE`」的两步写法**。机制与代价的完整论证在 `design.md` §3，本节只写实现口径。

### 5.1 三阶段认领与事务边界

**一次 tick 恒为三段，各开独立的短会话**（`docs/architecture.md` §5）：**① 认领（短事务）→ ② 调用（事务外）→ ③ 结果（第二个事务）**，认领事务在发起外部调用之前已经提交。

| 阶段 | 做什么 | 落点 |
|---|---|---|
| ① 认领 | `FOR UPDATE SKIP LOCKED` 挑出已到点的行，写 `attempt_count` 与 `next_attempt_at`，提交 | `db/repositories/*.py` |
| ② 调用 | 执行审查 / SMTP 投递 / IMAP 拉取（收割行**跳过**此步） | `providers/*/`，由 `flows/*.py` 编排 |
| ③ 结果 | 写目标行 + 删待审行；**只写** `status` / `last_error_class` / `last_smtp_code` / `sent_at` / `received_at` / `reviewed_at` | `db/repositories/*.py` |

- **数据库事务不得跨越网络调用**：单次调用受 `SMTP_TIMEOUT_SECONDS` / `IMAP_TIMEOUT_SECONDS` / `REVIEW_TIMEOUT_SECONDS` 约束（默认各 10 秒），`FR-15` 的 3 分钟由这几个上界与退避表共同推出（对应 `AC-40`）。
- **流的独立性**：五条流各自独立循环、独立间隔、独立分批，各自吞异常——单条流阻塞或抛异常都不影响其余各条。
- **保留期清理同构但语义不同**：它也走「分批 + `FOR UPDATE SKIP LOCKED`」，但动作是**删除**而非写状态（§5.5）。

### 5.2 认领谓词、退避表与条件 UPDATE

- **认领谓词**：`attempt_count <= 4` **且** `next_attempt_at <= now()`（`messages` 侧再加 `status = sending`、`direction = outbound`）。上限放宽到 `<= 4` 正是为收割支留出命中面。
- **退避表**（认领第 n 次时推 `next_attempt_at = now + backoff[n]`）：n=1 → **15s**、n=2 → **30s**、n=3 → **60s**；**n=4 不推**，因为这一行只剩终态一条路。这套数字由 `docs/research/02-runtime-and-scheduling.md` 定，**不得在实现时硬编码进业务流程**，由 `core/retry.py` 集中提供（`AC-9` 断言这三个差值）。
- **取时一律用应用侧的系统时间**：`datetime.now(UTC)` 由流程层取出后**作为参数绑定**写进 SQL（`next_attempt_at = t0 + backoff[n]` 里的 `t0` 就是认领事务取到的那个时刻），既不用 `func.now()`、也不在**认领路径**引入可注入时钟。理由不是「对齐两个时钟」——同一宿主机上应用与数据库读的是同一个内核时钟——而是**认领事务的取时点只有一处**：认领是短事务，一个 `t0` 同时用于谓词比较与退避写入，没有第二个时间源可漂移。因此 `core/retry.py` 的 `next_attempt_at(now, attempt)` 把 `now` 作为入参——时间是进入纯逻辑的唯一入口，纯逻辑不自行取时。集成用例的口径见 §7.2（直接改列值造态，不依赖时钟注入）。
- **行自身的时间列取该行写入时刻，不与 tick 级时刻共用**：认领/退避用的 `t0` 只服务认领事务；**入站拉取流（流③）在同一批内逐封各取一次写库时刻**，用于该封自己的时间列（`created_at` / `next_attempt_at`）——同一批内因此严格递增、批内先后即邮箱内顺序（`design.md` §2.5「回复入库时刻」，`FR-10` / `AC-15` 的 `ORDER BY received_at ASC, id ASC` 口径）。**排序键 `received_at` 走的是跨表搬运**：流④ 过审落库时把该待审行的 `created_at` 原样搬进 `received_at`（两列同源、不重新取 `now()`，见 `design.md` §2.5），所以同一批一次过审也不会让排序键同值。该流的**取时源可注入**（缺省真时钟），单测据此注入步进时钟、把「逐封各取一次」变成确定性断言（`tests/unit/test_fetch_inbound_timing.py`）：这只是把时间当参数传入，不改变生产路径的取时点数量。其余四条流的写库时刻口径不在本次收口范围。
- **`attempt_count` 与 `next_attempt_at` 只在两处写**：认领事务（阶段①）与**目标行建行时**（跨表搬运到 `messages` 必须重置为 `0` 与 `now()`）。结果事务**一律不碰这两列**。
- **状态写入一律带谓词的条件 UPDATE**，只允许 `design.md` §2.3 声明的起点（出站行 `sending → sent`、`sending → failed`、`sent → failed`；入站行只有 `received` 不迁移）；`rowcount = 0` 与撞唯一键 / 主键**按幂等忽略**（视为「这一封已经处理过」），不报错、不重复写。构造器写法与编译产物见 §2.9。
- **`5xx` 是例外**：直接置终态 `failed`，不等上限、不再消耗剩余重试（`AC-34` 的 `attempt_count` 为 1 由此成立）。

### 5.3 收割支

**定义**：第 4 次尝试**已被认领、结果事务未提交**的崩溃遗留行。

- **不递增计数、不推退避、不发起任何外部调用**，直接进阶段③ 把它推进终态：`messages` → `failed`；`pending_messages` → `rejected_messages` 且 `source = review_incomplete`。
- **必须存在的理由**：没有这一支，该行此后永远无人认领（`attempt_count < 4` 不再命中），对外永久停在「已提交」/「审查中」，而 `pending_messages` **不参与清理**、卡住即永久。
- **它不产生第 5 次投递**——`AC-37` 断言「捕获端没有收到第 5 次投递」正是靠这一点。
- **代价如实登记**：这一封的最终结果可能被记为失败，而 SMTP 其实已经成功。

### 5.4 五条流各自的幂等防线与重复投递窗口

五条流共用三阶段骨架，但**幂等防线各自不同**——实现时不可互相套用：

| 流 | 幂等防线 | 重复执行的后果 |
|---|---|---|
| ① 审查出站 | 认领时写 `attempt_count`；结果事务「写目标行 + 删待审行」在同一事务，撞主键按幂等忽略 | 审查无害（一期空实现恒通过），重跑只多一次调用 |
| ② 发信 | 同上，外加结果写入的**条件 UPDATE**（只允许 `sending` 起点，`rowcount = 0` 忽略） | **SMTP 可能重复投递一次**（调用已完成、结果未提交的窗口）；`AC-29` 要求并发抢同一批时捕获端只收到一封 |
| ③ 拉取入站 | `mailbox + uidvalidity + uid` 唯一约束 + 游标逐封同事务推进；撞唯一键计入 `duplicate` 并**继续推进游标** | 不产生第二条记录；游标回退时该封被唯一键挡住且**不得让冲突把游标推进一起回滚**（`AC-11` / `AC-39`） |
| ④ 审查回复 | 同流①，另加写回复行时 `parent_id` 外键违例按 `reply_target_gone` 丢弃 | 不产生重复回复行 |
| ⑤ 保留期清理 | 分批 `FOR UPDATE SKIP LOCKED`；与流② 正在投递的行相撞时，被跳过的行留到下一轮 | 结果事务写 0 行按幂等忽略、不报错 |

- **已登记的重复投递窗口**（`design.md` §3）：调用已完成、结果尚未提交时崩溃，若 `attempt_count < 4`，该行会被重新认领并重跑一次；若恰是第 4 次，则由收割支推进终态（不重跑）。这是接受而不消除的代价，缓解见 §8.2。
- **单实例也按并发安全写**：即使一期只跑一个实例，「两个并发执行体抢同一批」的情形在测试里必须成立（`AC-29`），实现时不得因「反正是单实例」而退回两步写法。

### 5.5 保留期清理的并发口径

- **判据与范围**：按各行的 `created_at`（**本行产生时刻**，`design.md` §2.5）判定；范围只覆盖 `messages` 与 `rejected_messages`——`pending_messages` 是纯队列，不参与清理。
- **删出站行必须排除仍有入站待审行指向它的行**（`NOT EXISTS`）：否则父行先被删会留下指向已删行的待审行，流④ 之后写回复行即撞 `parent_id` 外键，把用户的**正常回复误报成拒审**（`AC-22` 后半段的考点）。
- **并发口径**：每批按 `BATCH_SIZE` 取行并 `FOR UPDATE SKIP LOCKED` 后删除；与流② 并发时被跳过的行留到下一轮，写 0 行按幂等忽略、不报错。
- **回复不单独删**：由 `parent_id` 外键的级联清除，因此 `AC-22` 的「执行一次清理动作」就是「`await` 一次 tick」，不依赖任何外部定时配置。
- **间隔**：由 `POLL_CLEANUP_SECONDS`（默认 3600 秒）控制，属配置项、不硬编码（`NFR-2`）。

## 6. 配置项变更

### 6.1 本次变更的配置项

**本次不新增、不修改任何 `.env` 配置项。** 本特性的配置面已在前几轮提交里落地：`config.py` 的 **32 个字段**与 `.env.example` 里的 **32 个同名大写键**逐一对应，`.env.example` 另有 4 个不属于 `Settings` 的键（`POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` 供 compose 里的 postgres 容器 initdb，`VS_EMAIL_FORWARDING_IMAGE` 供 `compose.yaml` 选镜像 tag），合计 36 个键。六态重构（把内外状态统一成一套取值）**没有**引入新配置——状态取值是服务固定语义，按 `AGENTS.md` §7 的判据归 `constants.py`，**不得**为它新增 `.env` 项。

本次对配置层的改动只有两处**行为**（都不改 `.env` 键名或默认值）：

| 落点 | 本次动作 | 说明 |
|---|---|---|
| `src/vs_email_forwarding/config.py` | `新增` `validate_startup()` | 三条判据（§4.4）；只读已有字段，不新增字段 |
| `src/vs_email_forwarding/main.py` | `修改` | 在 lifespan 显式启动步骤调用 `validate_startup()`，**不放在 import 期** |
| `.env.example` | `修改` | 只做一致性复核：确认文件里的键与 `config.py` 字段一一对应、值与默认值一致；**不新增键** |

配置项清单的**唯一真源是 `design.md` 的「配置项清单」表**（缓存/日志、HTTP、数据库与 compose 初始化、发件与回复路由、SMTP、IMAP、六个轮询间隔、三个超时、批量、两项内容上限、回复分页、`VS_EMAIL_FORWARDING_IMAGE`），本节**不转抄**。任何配置项的增删改都必须先按 `AGENTS.md` §6 改写那张表，再回到本节。

一条边界提醒（`AGENTS.md` §8.2）：`10` 秒是既定间隔，但**不得**写死在代码里——六个 `POLL_*_SECONDS` 与 `BATCH_SIZE` 一律从 `config.py` 读，实现时不得出现字面量。

### 6.2 需同步的文件（含 `docs/configuration.md`）

| 文件 | 动作 | 本次要做的事 |
|---|---|---|
| `docs/configuration.md` | `新增` | 配置项清单文档：逐项写用途、默认值、是否必填、归属（`.env` 值 vs `constants.py` 的服务固定语义），以及启动即校验的三条判据。内容与 `design.md` 的配置项清单对齐、不另立一份取值 |
| `docs/README.md` | `修改` | ① §2 导航表加两行：「改配置项 → `configuration.md`」「查接口契约 → `api-contract.yaml`」；② §3 的「尚未创建的文档」清单**移除** `configuration.md`（保留其余四份） |
| `docs/api-contract.yaml` | `新增` | 对外契约唯一真源（§2.8 已列）；本次与 `tests/unit/test_openapi_contract.py` 互为校验对象 |
| `.env.example` | `修改` | 一致性复核（§6.1），不新增键 |
| `migrations/README.md` | `修改` | 三处就地改写：① 「约定」清单的第 3 条文件名约定（计划正文里也写成「第 23 条」，指的就是这一条）改为**序号前缀 + 下划线**（例 `0001_create_mail_tables.py`），删掉「保留 Alembic 自动生成的 revision 哈希前缀」；② 删掉「不要提前 `alembic init`」这段指引与「当前状态：尚未初始化」段，改为已初始化的现状；③ 「约定」清单的第 6 条迁移入口确认为 `deploy/migrate.sh` |
| `deploy/README.md` | `修改` | 从纯职责说明改为脚本清单：`migrate.sh` / `run.sh` / `check_archive.py` 各自的用途、入口、判据出处；写明 **D2-乙（产出方那一侧的打包脚本）本期不在范围、随 D13 推迟** |
| `docs/research/01-delivery-and-deploy.md` | `修改` | 按本轮裁决记两件事：**D2-甲** 三件脚本的落地位置与判据；**D2-乙 随 D13 推迟**。「落地位置」段的「待实现」列表相应收窄 |
| `docs/open-questions.md` | `修改` | D2 就地**收窄**为「剩余：产出方那侧脚本（构建 / tar 导出 / load / 构建输入传输），随 D13 推迟」；已定的 D2-甲从该条移除。**不要新增条目** |
| `README.md` | `修改` | ① 「当前状态」段的「需求阶段」表述改为「设计阶段已收口、进入实现」；② 「生产部署（现状）」段的「`deploy/` 目前只有一份职责 `README.md`」改为三件脚本已落地；③ 目录结构块补 `constants.py` 一行 |
| `src/vs_email_forwarding/**/__init__.py` | `修改` | §2.10 列出的 **7 个包占位文件**的过时 docstring（逐个指向当前真源）；`providers/review/__init__.py` 里「真实审查能力何时补上仍未定」这半句**保留** |
| `tests/integration/README.md` | `修改` | 「覆盖重点」第 1 条仍写着旧词「待审 → 已排队 → 已发送 → 已收到 / 拒审 / 终态失败」——与本特性已拍板的六态取值（`constants.MESSAGE_STATES`）不一致，按 `docs/README.md` §4 的「文档与代码不一致时以代码为准」就地改写；同时补上集成夹具的实际落点（`tests/integration/conftest.py`）与测试库准备命令 |
| `tests/fixtures/README.md` | `修改` | 从「当前阶段为空」改为实际内容清单（DSN 样本、MDN 样本、回复样本、原邮件样本的落点与命名），并写明样本里一律只用 `example.com` 占位地址 |
| `CHANGELOG.md` | `修改` | 实现完成后按 commitizen 的口径补一条版本条目（提交信息用 Conventional Commits + 中文，见 `AGENTS.md` §9） |
| `pyproject.toml` | `修改` | 唯一的改动是 `[[tool.mypy.overrides]]` 给 `asyncpg` / `imapclient` 两个模块各一条**精确放行**（理由已在 `docs/research/03-dependencies-and-toolchain.md` 登记）；**不得**放宽 ruff / mypy 的任何其他开关 |

## 7. 测试策略

### 7.1 单测与集成测试的分工

分工的判据只有一条：**这条断言需不需要真实数据库或真实邮件服务**。需要就走 `tests/integration/`，不需要就走 `tests/unit/`；`docs/research/02-runtime-and-scheduling.md` 已定「集成测试使用真实 PostgreSQL，不以 mock 替代数据库」——抢占与事务用的是数据库语义，mock 验证不了。

先说清本特性**不写**哪种测试：五条流的循环与调度器（`flows/scheduler.py`）**不做端到端进程测试**。它是「每 N 秒 `await` 一次 tick」的封装，真实行为由「单次 tick 是一个普通 async 函数、测试里直接 `await`」这条可测性结论覆盖（同上册）；为调度器另写 sleep 轮询测试只会把门禁拖慢、且引入不稳定性。**tick 本身在集成测试里直接调用，不等 10 秒。**

| | `tests/unit/` | `tests/integration/` |
|---|---|---|
| 依赖 | 无数据库、无网络、无 Docker；允许 `fastapi.testclient.TestClient` 走进程内 HTTP | 真实 PostgreSQL（测试库 `vs_email_forwarding_test`）+ 假 SMTP / 假 IMAP / 可替换的假审查（§7.2） |
| 覆盖的层 | `core/`、`constants.py`、`config.py` 的校验判据、`schemas/` 的校验与序列化、`providers/*/base.py` 的中性对象、`db/repositories/` 的**语句编译产物** | `db/` 的约束与并发语义、三条 HTTP 端点、五条流各自的推进与幂等、跨表搬运、清理与级联 |
| 典型断言 | 退避算术、六态判定优先级、`Message-ID` 解析、DSN / MDN 结构判定、422 清单、时间格式、`FOR UPDATE SKIP LOCKED` 是否出现在编译出的 SQL 里 | 唯一约束真的挡住重复入站、并发抢占只投一封、游标回退不产生第二条、清理的 `NOT EXISTS`、`next_cursor` 翻页全集一致 |
| 不放在这 | 任何真实 PostgreSQL / SMTP / IMAP 用例 | 纯逻辑断言；也不放「必须真杀进程」的用例（口径见 §7.4） |

五条**跨层**的静态检查放在单测目录里，它们不连数据库、只读仓库文件：

- **契约不漂移**（`tests/unit/test_openapi_contract.py`）：把 `create_app().openapi()` 与 `docs/api-contract.yaml` 逐路径、逐字段、逐 `enum` 比对。
- **三方取值一致**（`tests/unit/test_constants_consistency.py`）：`docs/api-contract.yaml` 的 `MessageState` / `BodyFormat` 枚举 == `constants.py` 的取值集合 == 迁移文件与 `db/models.py` 里 `CHECK` 的取值集合；另断言 `constants.py` **零项目内 import**（共享叶子层的硬约束，靠静态读源码判定，`docs/architecture.md` 硬规则第 4 条）。
- **抢占写法唯一**（`tests/unit/test_repository_statements.py`）：见 §7.3 的 `AC-26` 一行。
- **配置只从配置层读**（`tests/unit/test_config_hygiene.py`）：见 §7.3 的 `AC-27` 一行。
- **空实现被显式标注**（`tests/unit/test_review_placeholder.py`）：见 §7.3 的 `AC-25` 一行。

### 7.2 集成测试的前置与夹具

**前置（每条集成用例都假定已就绪）**

1. `bash scripts/dev-up.sh` —— 起 `compose.dev.yaml` 里的 PostgreSQL 17 与 GreenMail（SMTP 3025 / IMAP 3143）。假 SMTP / 假 IMAP 用进程内的轻量替身（见下），GreenMail 只作为「真回路」的兜底验证。
2. 测试库**不再手工建**：`T-33` 的夹具在每次 pytest 运行自建一个**运行级隔离库**（库名形如 `vs_email_forwarding_test_<pid>_<hex8>`，进程号 + 随机后缀，并行不撞），建库后跑 `alembic -c migrations/alembic.ini upgrade head` 建表，运行结束 `DROP DATABASE ... WITH (FORCE)` 删库。开发库 `vs_email_forwarding_dev` 从头到尾**不被连接**（`tests/integration/conftest.py` 的 `test_database` 夹具）。
3. 连接串来源：显式的 `VS_EMAIL_FORWARDING_TEST_DATABASE_URL`（若设置）优先，否则用配置层的 `DATABASE_URL`——两种来源都**只当服务器坐标用**（host / port / 用户 / 口令），库名一律由夹具生成，**不改仓库里的配置文件**（`docs/research/02-runtime-and-scheduling.md` 的「库隔离」）。

**夹具（`tests/integration/conftest.py`）**

| 夹具 | 形态 | 职责 |
|---|---|---|
| `settings_override` | fixture | 把配置指向测试库与假服务；必要时覆盖 `POLL_*` / `BATCH_SIZE` / `REPLIES_PAGE_SIZE` 这类与用例规模有关的项 |
| `engine` / `session_factory` | fixture | 每个用例自建一套异步引擎并在结束 `dispose()`；`session_factory` 是绑定该引擎的短会话工厂（`expire_on_commit=False`）。另有 autouse 夹具在每个用例前后丢弃 `db/session.py` 的进程级单例引擎（跨用例不共享进程级状态），并由清表夹具对隔离库做 TRUNCATE 前后清表 |
| `schema` | fixture（session 级） | `test_database` 的具名别名：本次运行独占的隔离测试库（建库 → `alembic -c migrations/alembic.ini upgrade head` → 运行结束 `DROP DATABASE ... WITH (FORCE)`）；逐用例的前后清表由 autouse 的 `clean_tables` 承担。**清表用真实 DDL 级操作（TRUNCATE，表名取自 ORM 元数据），不用 mock** |
| `fake_review` | 可替换实现 | 三种模式：恒 `pass`（默认，等价空实现）、恒 `reject`、抛异常；通过 `providers/review/factory.py` 的依赖覆盖 / monkeypatch 注入，用于 `AC-6` / `AC-12` |
| `smtp_capture` | 进程内假 SMTP + 捕获端 | 记录每次投递的**次数**、`From` / `Reply-To` / 收件人与原始报文；可脚本化为「连续 `4xx`」「持续 `5xx`」「接受连接后挂起不返回」 |
| `imap_fake` | 进程内假 IMAP | 支持注入任意邮件样本、按 `UIDVALIDITY` / `UIDNEXT` 给出增量、支持把游标回退（`AC-39`） |
| `fixtures_dir` | 路径夹具 | 指向 `tests/fixtures/`，样本文件在那里（DSN / MDN / 回复 / 原邮件），代码与数据分离 |

**时间推进的口径**：集成用例**不真的等 15 / 30 / 60 秒**，而是每轮认领前把目标行的 `next_attempt_at` 置到「已到点」（这是设计允许的：退避值由 `core/retry.py` 单测断言，见 `AC-9`），再 `await` 一次 tick。这样多轮重试在毫秒级跑完，且断言的是**状态与列值**，不是墙钟。

**样本里只出现占位地址**（`example.com` 一类），不得出现真实邮箱地址、主机名或凭据（`AGENTS.md` §8.4）。

### 7.3 逐条 `AC-n` 的测试落点

下表覆盖 `AC-1` … `AC-41`，**41 条一条不少**；每条给出可判定真假的方式（命令 / 测试名 / 步骤）与**期望值来源**——`docs/testing.md` 的 `R2` 要求期望值不得来自被测代码，所以每行都必须能说出它的 oracle 在哪。命令一律指 `uv run pytest <路径>::<测试名>`，全量门禁是 `bash scripts/check.sh`。

| AC | 覆盖 | 测试与文件 | 期望值来源 | 用例名与断言要点 |
|---|---|---|---|---|
| `AC-1` | `FR-1` | `tests/integration/test_api_contract.py` | `spec.md` 的 `FR-1` + `docs/api-contract.yaml` 的请求 / 响应字段定义 | `test_submit_then_query`：`POST /v1/messages`（单收件人 + 主题 + `text` 正文）返回 2xx 且含 `id`；`GET /v1/messages/{id}` 立刻返回 `found: true`、`status == "submitted"` |
| `AC-2` | `FR-1` | `tests/integration/test_api_contract.py` | `docs/api-contract.yaml` 的必填字段清单（缺 `recipient` 即 422） | `test_missing_recipient_422`：缺收件人 → 422，响应体出现收件人字段名；断言提交前后 `pending_messages` 行数不变 |
| `AC-3` | `FR-1` | `tests/integration/test_api_contract.py` | `docs/api-contract.yaml` 的单收件人约束（`recipient` 是单值、不是数组） | `test_two_recipients_422`：两个收件人（列表与逗号分隔两种形态）→ 422 且不落库 |
| `AC-4` | `FR-2` | `tests/integration/test_send_flow.py` | `AGENTS.md` §8.3 + `MAIL_FROM_ADDRESS` 配置值（期望值是配置里那个地址，不是请求里给的） | `test_from_fixed_from_config`：请求体多带一个发件人字段（未知字段）→ 仍能发出，`smtp_capture` 捕获到的 `From` 等于 `MAIL_FROM_ADDRESS`，且不等于请求里给的地址 |
| `AC-5` | `FR-2` | `tests/integration/test_send_flow.py` | `AGENTS.md` §8.3 + `REPLY_TO_ADDRESS` 配置值 | `test_reply_to_written`：捕获到的报文含 `Reply-To`，其值等于 `REPLY_TO_ADDRESS` |
| `AC-6` | `FR-3` | `tests/integration/test_review_outbound_flow.py` | `design.md` §3.2 的状态落点表：拒审 → `rejected_messages`，**不产生任何投递** | `test_reject_no_smtp`：`fake_review` 恒 `reject` → `await` 一次流① tick 与一次流② tick，`smtp_capture` 投递次数为 `0`；状态为 `rejected` |
| `AC-7` | `FR-4` | `tests/integration/test_send_flow.py` | `spec.md` 的 `FR-4` + `design.md` §2.3 的合法迁移（`sending → sent`） | `test_outbound_end_to_end`：真实 PostgreSQL + 假 SMTP 捕获，跑完流① 与流② 各一次 tick → 捕获端收到该邮件，`messages.status == "sent"` |
| `AC-8` | `FR-5` | `tests/integration/test_send_flow.py` | `AGENTS.md` §8.7 与 `docs/research/02-runtime-and-scheduling.md` 的重试语义（合计 4 次尝试后终态） | `test_transient_exhausts_attempts`：假 SMTP 连续 `4xx` → 第 4 次尝试失败后置 `failed`，`attempt_count == 4`，`last_error_class == "transient"`，`last_smtp_code` 为该 `4xx` 码 |
| `AC-9` | `FR-5` | `tests/unit/test_retry_policy.py` + `tests/integration/test_send_flow.py` | `constants.RETRY_BACKOFF_SECONDS` 的 15 / 30 / 60 | 单测 `test_backoff_table`：`now + backoff[n]` 的差值为 15 / 30 / 60 秒；集成 `test_backoff_written_on_claim`：每轮认领后读回的 `next_attempt_at` 与认领时刻的差值分别约为 15 / 30 / 60 秒 |
| `AC-10` | `FR-6` | `tests/integration/test_fetch_inbound_flow.py` | `design.md` §3.4 的退信判定（`multipart/report` + `message/delivery-status`） | `test_dsn_sets_failed`：注入 DSN 样本（`multipart/report` + `message/delivery-status` + 内嵌原邮件）→ 跑一次流③ tick → 对应出站行 `status == "failed"` |
| `AC-11` | `FR-7` | `tests/integration/test_fetch_inbound_flow.py` | `design.md` §2.2 的入站唯一键 + §3.4 的 `duplicate` 计数口径 | `test_inbound_dedup_by_uid`：同一 `mailbox + uidvalidity + uid` 连续跑两次流③ tick → 库中只有一条记录，第二次计入 `duplicate` |
| `AC-12` | `FR-8` | `tests/integration/test_fetch_inbound_flow.py` + `test_review_inbound_flow.py` | `design.md` §3.5 的拒审落点（`rejected_messages`） | `test_rejected_reply_not_queryable`：`fake_review` 恒 `reject` → 回复进 `rejected_messages`，`GET .../replies` 不返回它（`found: true` + 空集合） |
| `AC-13` | `FR-9` | `tests/integration/test_fetch_inbound_flow.py` | `design.md` §3.4 的十一类丢弃原因与「只记条数」口径 | `test_unmatched_inbound_discarded`：注入关联不上的入站邮件 → 三张邮件表行数均不增；`caplog` 抓到一条 `inbound_discarded`，事件里只有计数（`no_match == 1` 等） |
| `AC-14` | `FR-9` | `tests/integration/test_fetch_inbound_flow.py` | `design.md` §3.4 的 MDN 剔除规则 | `test_mdn_dropped_reply_kept`：同时注入 MDN 样本（含 `message/disposition-notification`）与一封 `In-Reply-To` 指向出站行的普通回复 → 回执不入库（`mdn` 计数为 1），`GET .../replies` 只返回那 1 条 |
| `AC-15` | `FR-10` | `tests/integration/test_api_contract.py` | `design.md` §2.5 的排序键（`received_at ASC, id ASC`） | `test_replies_stable`：连续调用 `GET .../replies` 两次 → 两次的回复 ID 序列完全一致（顺序也一致） |
| `AC-16` | `FR-10` | `tests/integration/test_api_contract.py` | 同上 + 测试自己构造的 3 条递增 `received_at` | `test_replies_sorted_asc`：写入 3 封回复（`received_at` 递增）→ 返回 3 条且按 `received_at ASC, id ASC` 排列 |
| `AC-17` | `FR-11`、`NFR-7` | `tests/integration/test_api_contract.py` + `tests/unit/test_schemas.py` | `design.md` §2.3 的六态与 §4.1 的时间格式（UTC RFC 3339 六位小数 + `Z`） | `test_three_phase_states`：提交后立即查得 `submitted` → 跑一次流② tick 后得 `sent` → 注入退信并跑一次流③ tick 后得 `failed`，三次取值互不相同；单测 `test_timestamp_format`：所有时间字段形如 `%Y-%m-%dT%H:%M:%S.%fZ`，且与库中 UTC 值一致 |
| `AC-18` | `FR-12` | `tests/integration/test_api_contract.py` | `design.md` §2.3 / §3.2 的拒审终态 | `test_rejected_state_queryable`：恒 `reject` 的假审查下，`GET /v1/messages/{id}` 返回 `rejected`，且该 ID 不出现在任何 `sent` 结果里 |
| `AC-19` | `FR-13`、`NFR-9` | `tests/integration/test_send_flow.py` | `design.md` §3.7 的恢复口径（状态字段 + `next_attempt_at`） | `test_restart_resumes`：留一行 `sending` 未处理，**在同一个测试里重新构造应用与引擎**（不真杀进程，口径见 §7.4）→ 跑流② tick 后该行推进到 `sent`，期间无需人工干预 |
| `AC-20` | `FR-14`、`NFR-3` | **交付阶段验证，不入门禁** | `spec.md` 的 `FR-14` / `NFR-3`（60 秒上界）；交付阶段验证，不入门禁 | 在与公网隔离、可达内网镜像源的服务器上 `bash deploy/run.sh` → 60 秒内 `GET /healthz` 返回 200。本地不可复现（本地开发机不满足隔离前提），登记为交付检查项，见 §9.1 的 S-8 |
| `AC-21` | `FR-15` | `tests/unit/test_retry_policy.py` + `tests/integration/test_send_flow.py` | `spec.md` 的 `FR-15`（180 秒上界）+ 退避表与超时上界（`constants` 与配置默认值）的纯算术 | 单测 `test_worst_case_within_180s`：最坏路径 = 4 × 超时上界 + 15 + 30 + 60 ≤ 180 秒（算术）；集成 `test_within_three_minutes`：提交后不干预、假 SMTP 成功，真实等待约 2× 配置间隔后断言 `sent` 出现的时刻早于「提交 + 180 秒」（手法见 §7.4） |
| `AC-22` | `NFR-8` | `tests/integration/test_cleanup_flow.py` | `NFR-8` 的 30 天 + `design.md` §3.6 的 `NOT EXISTS` 口径 | `test_cleanup_scope_and_not_exists`：写入 31 天前的一对邮件 + 一条 31 天前的拒审行 + 一条 29 天前的记录 → 跑一次清理 tick，前三条查不到、29 天前那条仍在；**另写一条 31 天前但仍有入站待审行指向它的出站行，断言清理后该行仍在** |
| `AC-23` | `NFR-2` | `tests/unit/test_scheduler_intervals.py` | 配置项 `POLL_SEND_SECONDS` 自身的值（改配置后重建即为新值） | `test_interval_comes_from_config`：调度器构造时读到的间隔 == 配置值，改配置后重建即为新值；同文件 `test_no_interval_literals`：AST 扫描断言间隔与批量不以字面量出现在源码中（口径与 `spec.md` 的 `AC-23` 一致，不测墙钟） |
| `AC-24` | `NFR-5` | `tests/integration/test_send_flow.py` | 测试自己注入的正文片段与收件人地址（期望是「不出现」的否定断言） | `test_logs_no_content`：投递一封正文含可识别片段、收件人可识别的邮件 → `caplog` 全量文本既不含该正文片段，也不含收件人地址（否定断言，`docs/testing.md` §4.5 明确允许） |
| `AC-25` | `NFR-6` | `tests/unit/test_review_placeholder.py` | `NFR-6` 明列的两个关键词（「空实现」「永远通过」） | `test_empty_implementation_annotated`：读 `providers/review/base.py` 与 `always_pass.py` 开头注释，断言同时出现「空实现」与「永远通过」；再读 `docs/security.md` 的安全边界一节，关键词可检索到该标注 |
| `AC-26` | `NFR-1` | `tests/unit/test_repository_statements.py` | `AGENTS.md` §8.1 的抢占硬约束（`FOR UPDATE` 与 `SKIP LOCKED` 必须同时出现） | `test_no_two_step_claim`：① 用静态 AST 扫描 `db/repositories/` 的认领函数，断言不存在「同一次认领里先 `select` 出候选、再单独 `update` 写状态」的写法；② 断言认领语句编译出的 SQL 同时含 `FOR UPDATE` 与 `SKIP LOCKED`。配套的静态检查见 §7.1 第三条 |
| `AC-27` | `NFR-4` | `tests/unit/test_config_hygiene.py` | `AGENTS.md` §7 的配置读法 + `AC-27` 自己给定的 `git ls-files` 清单 | `test_no_direct_env_access`：遍历 `src/` 的 AST，断言除 `config.py` 外没有任何模块 import `os` 后访问 `environ`、也没有第二处读 `.env`；`git ls-files` 含 `.env.example` 且不含 `.env`。连接参数与地址只经 `config.get_settings()` |
| `AC-28` | `NFR-10` | `tests/integration/test_fetch_inbound_flow.py` | 测试本次注入的 3 封（计数 3 由测试自己定）+ 否定断言 | `test_discard_log_only_counts`：连续丢 3 封关联不上的入站邮件 → `caplog` 中存在合计 3 的条数记录（断言的是**事件名与受控计数字段**，`docs/testing.md` §4.5），且日志文本不含任何发件人地址、主题或 UID |
| `AC-29` | `NFR-1`、`FR-4` | `tests/integration/test_repositories.py` + `test_send_flow.py` | `NFR-1` 的「恰好一次」+ 捕获端的投递记录 | `test_concurrent_claim_single_delivery`：两个并发执行体同时抢占同一批待发行（两个 `asyncio` 任务各开自己的会话）→ 捕获端只收到一封，库里 `status == "sent"` |
| `AC-30` | `FR-5` | `tests/integration/test_send_flow.py` | 假 SMTP 自己返回的那段文本与其中的地址（期望是「不出现」） | `test_error_has_no_free_text`：假 SMTP 返回一段含收件人地址的原始回复文本 → 进终态失败后，库中 `last_smtp_code` 与 `last_error_class` 有值，且**全表任何列都不出现那段文本或该地址** |
| `AC-31` | `FR-10` | `tests/integration/test_api_contract.py` | `design.md` §4.1 的 `found` 语义（存在但无回复 = 成功 + 空集合） | `test_existing_without_replies`：对存在但无回复的邮件调用 `GET .../replies` → HTTP 成功、`found: true`、`replies` 为空集合 |
| `AC-32` | `FR-2`、`NFR-4` | `tests/unit/test_settings_validation.py` + `tests/integration/test_startup_validation.py` | `design.md` §4.4 的三条判据（校验信息只出现配置项名） | 单测三条判据各自单独制造一次违规；集成测试用子进程启动应用（`subprocess` 跑 uvicorn / 直接调用 `validate_startup()` + 断言进程非零退出），断言**非零退出且不监听端口** |
| `AC-33` | `FR-1` | `tests/integration/test_api_contract.py` | `MAX_SUBJECT_CHARS` / `MAX_BODY_BYTES` 配置值（改配置后上限随之变） | `test_over_limit_422`：主题超 `MAX_SUBJECT_CHARS`、正文超 `MAX_BODY_BYTES` 各一例 → 422 且不落库；断言上限取自配置项（改配置后上限随之变） |
| `AC-34` | `FR-5` | `tests/integration/test_send_flow.py` | `design.md` §3.3 的 `5xx` 例外（直接终态、`attempt_count == 1`） | `test_permanent_fails_immediately`：假 SMTP 持续 `5xx` → 第一次尝试后即 `failed`，`attempt_count == 1`，`last_error_class == "permanent"`；「无退避等待」判作**终态在同一次 tick 内到达、之后无退避等待与再次尝试**（`next_attempt_at` 由认领事务无条件推 `now + 退避`、结果事务不碰该两列——`design.md` §2.4 / §3.3；与 `spec.md` `AC-34` 原文口径一致，`T-35` 的用例已按此落地并在用例内注明） |
| `AC-35` | `FR-10`、`NFR-8` | `tests/integration/test_api_contract.py` | `design.md` §4.1 的 `found: false`，与 `AC-31` 的 `found: true` + 空集合可区分 | `test_not_found_vs_no_replies`：不存在的 ID 调两个查询端点 → 都是成功状态码，响应体 `found: false`；与 `AC-31` 的 `found: true` + 空集合**可区分** |
| `AC-36` | `FR-11` | `tests/integration/test_api_contract.py` | `design.md` §2.3 的合法迁移图（不得出现向后步骤） | `test_state_monotonic`：**由测试显式推进**——`await` 一次流① tick 后查询、再 `await` 一次流② tick 后查询，记录取值序列 → 不出现向后步骤，且至少观测到 `reviewing` 与 `sending`（不依赖后台高频轮询，`docs/testing.md` R12 要求确定性） |
| `AC-37` | `FR-5`、`NFR-1` | `tests/integration/test_send_flow.py` | `design.md` §3 的收割支定义（`attempt_count == 4` 且 `sending`）+「不产生第 5 次调用」 | `test_reaper_after_fourth_attempt`：**确定性造态**——假 SMTP 持续 `4xx`，跑到第 4 次尝试被认领后，直接构造遗留行（`attempt_count == 4`、`status == "sending"`、`sent_at` 为空），`await` 一次流② tick → 该行进终态 `failed`，且 `smtp_capture` 投递次数**没有**增加。完整口径见 §7.4；`spec.md` 的 `AC-37` 已就地改写为同一口径 |
| `AC-38` | `FR-6` | `tests/integration/test_fetch_inbound_flow.py` | `design.md` §3.4 的 `dsn_target_not_sent`（不置状态、不打断重试） | `test_dsn_target_not_sent`：对仍在重试等待的行（`sending`、`sent_at` 为空）注入关联它的 DSN 并跑一次流③ tick → 该行**不变成** `failed`，`next_attempt_at` / `status` 不变（重试照常），本次计入一条 `dsn_target_not_sent` |
| `AC-39` | `FR-7`、`NFR-1` | `tests/integration/test_fetch_inbound_flow.py` + `test_repositories.py` | `design.md` §3.4 的游标推进规则（冲突不得把推进一起回滚） | `test_cursor_rollback_advances`：把游标回退到已处理过的 UID 再跑一次流③ tick → 库中不产生第二条记录，`imap_cursors.last_uid` **仍然推进**，本次计入一条 `duplicate`。完整口径见 §7.4 |
| `AC-40` | `FR-15` | `tests/integration/test_send_flow.py` | `SMTP_TIMEOUT_SECONDS` / `REVIEW_TIMEOUT_SECONDS` 配置值 | `test_timeout_bounded`：假 SMTP 接受连接后挂住不返回 → 单次尝试在 `SMTP_TIMEOUT_SECONDS` 内结束（断言耗时上界），执行体不被无限期占用，该邮件仍能在 3 分钟内进入 `sent` / `failed`。完整口径见 §7.4 |
| `AC-41` | `FR-10` | `tests/integration/test_repositories.py` + `test_api_contract.py` | 测试自己写入的 250 条 + `REPLIES_PAGE_SIZE`（100） | `test_keyset_250_replies`：写入 250 条回复、每页 100 条 → 用 `next_cursor` 逐页取完得 250 条且顺序与全集一致；**翻页中途插入一条新回复**，已取过的页不重不漏，继续翻页最终仍取到全集（`found: true`）。完整口径见 §7.4 |

**不入门禁的交付阶段条目**：`AC-20`（隔离环境 + 容器启动）不在本地自动化门禁里——本地开发机不满足「与公网隔离」这一前提，把它硬塞进 `pytest` 只会得到一条永远跳过或永远假的用例。它按 §9.1 的 S-8 在真实服务器上按步骤验证，验收结果同样回填 `spec.md` §9。

### 7.4 需要特别写清手法的用例（`AC-21` / `AC-23` / `AC-37` / `AC-39` / `AC-40` / `AC-41`）

这六条容易被写成「看起来过了、其实没测到」的用例，手法逐条钉死：

**`AC-21` 真实等待与纯算术分开写。** 集成那条只走成功路径：提交后不干预、假 SMTP 成功，真实等待约 2× `POLL_*_SECONDS`（默认 10 秒）后断言 `sent` 出现，且出现时刻早于「提交时刻 + 180 秒」——这是**唯一允许真实等待**的用例，断言的是「不干预也能在 3 分钟内走完」，不是为了跑满 4 次重试。退避路径**不真等**：最坏路径由 `test_retry_policy.py` 的纯算术覆盖（4 × 超时上界 + 15 + 30 + 60 ≤ 180 秒），期望值全部来自 `constants` 与配置默认值。

**`AC-23` 不测墙钟。** `spec.md` 的 `AC-23` 已就地改写为「间隔取自配置、代码中无间隔字面量」：单测断言「调度器构造时读到的间隔 == 配置值」，改配置后重新构造即为新值；同文件的 AST 扫描断言 `POLL_*` / `BATCH_SIZE` 不以字面量出现在源码里。原先的「实际两次处理之间的间隔随之改变」在确定性口径下不可观测（会引入时间相关用例，违 `docs/testing.md` R12），因此不再作为判定对象。

**`AC-37` 确定性造态，不是真杀进程。** 测试里**不**做「fork 子进程 + kill + 重启」。手法是确定性构造崩溃窗口的**遗留状态**：

1. 假 SMTP 持续返回 `4xx`，按 §7.2 的时间推进口径跑满 3 次尝试，使该行 `attempt_count == 4`、`status == "sending"`；
2. 直接构造「第 4 次尝试已认领、结果事务未提交」的遗留行——即让该行保持 `attempt_count == 4` 且 `status == "sending"`、`next_attempt_at` 已到点（这正是崩溃窗口留下的形态）；
3. `await` 一次流② tick → 断言该行进终态 `failed`；
4. 断言**捕获端的 SMTP 调用次数与第 2 步结束时相同**——即收割支没有发出第 5 次投递。

这样既验到了「不得永久停在投递中」，也验到了「不产生第 5 次调用」，且不依赖进程生命周期（真杀进程会让用例变得不稳定，且断言的是「重启后能恢复」而不是「收割支的行为」，两者不是同一条 `AC`）。

**`AC-39` 游标回滚**：先把一封入站邮件正常处理完（库里 1 条、游标推到该 UID），再**把 `imap_cursors.last_uid` 回退到处理前的值**，再跑一次流③ tick。断言三条：

1. 库中**没有**第二条记录（唯一键挡住了重复插入）；
2. `imap_cursors.last_uid` **仍然推进**到该封的 UID——撞唯一键**不得**把游标推进一起回滚，否则每一轮都会重撞同一封、入站永久停摆；
3. 本次处理计入一条 `duplicate`（受控丢弃原因，`constants.DISCARD_REASONS`）。

**`AC-40` 假 SMTP 挂起**：假服务器接受 TCP 连接后**不返回任何响应**（既不回码也不关连接）。断言单次尝试在 `SMTP_TIMEOUT_SECONDS` 内结束（耗时断言带合理容差，断言的是「有上界」而不是精确值），且执行体没有被永久占用——后续 tick 仍能继续跑，该邮件最终进入 `sent` 或 `failed`。假审查实现同样要能挂起，因为 `REVIEW_TIMEOUT_SECONDS` 走的是同一条「超时按抛异常处理」的路径。

**`AC-41` 250 条回复的 keyset 翻页**：直接写入 250 条回复行（`received_at` 递增、`id` 稳定），`REPLIES_PAGE_SIZE = 100`：

1. 用 `next_cursor` 逐页取完 → 累计 250 条，顺序与按 `received_at ASC, id ASC` 排出的全集完全一致；
2. **在取完第 1 页之后、取第 2 页之前插入一条新回复**（其 `received_at` 排在第 2 页范围内）；
3. 断言：已取过的第 1 页**不重**（不出现重复 ID）、也不因插入而**漏**（第 1 页的 100 条都还在结果里）；继续翻页最终仍取到**新插入那封之后**的全集（新插入的回复本身也必须在后续页里被取到）。


## 8. 风险与缓解

### 8.1 实现期风险

只列**真实存在**的风险与缓解；每条都指向它的判据出处，不在这里重新论证。

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| **假 SMTP / 假 IMAP 与真实服务的语义偏差**：GreenMail 与进程内替身都不完全等价于企业邮箱（STARTTLS 行为、UID 语义、DSN 生成方式、超时表现） | 集成测试全绿但真实联调失败 | 假替身只承担「回路与幂等」的验证；`docs/research/05-dev-and-build-environment.md` 已登记 GreenMail 的两条已知差异（按账号分信箱、`auth.disabled`）；真实参数联调是 `docs/open-questions.md` B2，属交付后动作 |
| **把 `10` / `100` / `3600` 这类数值写死进代码**（`AGENTS.md` §8.2、`NFR-2`） | 间隔与批量不可配，`AC-23` 直接失败 | 六条流的循环、批量、分页一律从 `config.py` 取值；`AC-23` 用「改配置即生效」反证，代码里不得出现字面量 |
| **用两步写法（先 `SELECT` 判断、再 `UPDATE`）实现认领**（`AGENTS.md` §8.1） | 并发下重复投递，`AC-26` / `AC-29` 失败 | 认领一律 `select(...).with_for_update(skip_locked=True)`（写法与编译产物见 §2.9）；`AC-26` 的静态检查 + `AC-29` 的并发用例双向兜住 |
| **结果事务的 `rowcount = 0` 被当成错误处理** | 幂等窗口内重跑时报错、留下无法自愈的行 | 明确口径：`rowcount = 0` 与撞唯一键 / 主键**按幂等忽略**（`design.md` §3 阶段③）；`AC-11` / `AC-29` 断言的是「没有副作用」，不是「没有异常」 |
| **跨表搬运时重复写目标行 / `created_at` 被重新取 `now()` 覆盖** | 出站行的 30 天保留期被推迟；`AC-17` 的时间字段与库值不一致 | 搬运时 `created_at` 原样搬运（`design.md` §2.5）、`attempt_count` 与 `next_attempt_at` 重置为 `0` 与 `now()`（§2.4）；撞唯一键时只删待审行、不重复写目标行；`AC-11` / `AC-22` 覆盖 |
| **游标推进与冲突处理写反**：撞入站唯一键时把「游标推进」一起回滚 | 每轮重撞同一封、入站永久停摆（`AC-39` 的考点） | 单封的分拣落库与「把 `last_uid` 推到该封 UID」在**同一个事务**里完成（`design.md` §3.4）；`AC-39` 专门断言「冲突不挡推进」 |
| **契约两处漂移**：`docs/api-contract.yaml` 与 FastAPI 生成的 OpenAPI 各说一套 | 字段 / 枚举悄悄不一致，调用方按错的那份实现 | `tests/unit/test_openapi_contract.py` 逐字段比对；`tests/unit/test_constants_consistency.py` 断言契约枚举 == `constants.py` == 迁移与 ORM 的 `CHECK` 三方一致 |
| **`constants.py` 被当成便利模块，开始 import 项目内模块** | 破坏 `docs/architecture.md` 硬规则第 4 条（共享叶子层），`db/` 的反向依赖会随之出现 | `test_constants_consistency.py` 静态断言该文件零项目内 import；`db/` 的 `CHECK` 文本由迁移与 ORM 各持一份，**不**去 import 该模块 |
| **集成测试依赖外部环境（Docker 起得来 + PostgreSQL 可达——隔离测试库由夹具自建）** | 换机器后集成用例直接失败，容易被误判成代码问题 | §7.2 的三步前置写进 `tests/integration/README.md`；测试库连接串通过环境变量覆盖（不改仓库配置）；清表只清测试库 |
| **测试用库与开发库串号**：跑集成测试时 `.env` 恰好指向开发库 | 洗掉开发数据 | 集成夹具**不读** `.env` 的 `DATABASE_URL`，只认显式的测试库环境变量；库名以 `_test` 结尾做一次断言兜底（`docs/research/02-runtime-and-scheduling.md` 的「库隔离」） |
| **异步引擎 / 会话在用例之间泄漏** | 用例随机挂起或连接池耗尽，表现为「偶尔失败」 | 每个用例自建引擎并在结束时 `dispose()`；夹具按 `docs/architecture.md` §5 的口径收发会话，不在用例之间传递会话 |
| **审查空实现在测试里替换不干净**（`AC-6` / `AC-12` 依赖它） | 假审查漏进别的用例，或反例用例其实跑的是空实现 | 替换点固定在 `providers/review/factory.py`（`design.md` §4.2 已定工厂选取实现）；夹具显式选择「恒 `pass` / 恒 `reject` / 抛异常」三种模式，反例用例在断言前先断言替换已生效 |
| **`deploy/` 三件脚本触碰未定项**（D2-乙 / D13） | 写出按「产出方已定」假设的脚本，做完就得返工 | 本期只写 **D2-甲**（`migrate.sh` / `run.sh` / `check_archive.py`）；产出方那一侧的打包脚本**不写**，并在 `deploy/README.md` 与 `docs/research/01-delivery-and-deploy.md` 写明随 D13 推迟 |
| **`pyproject.toml` 的 mypy 放行被顺手扩大** | 类型检查形同虚设（`AGENTS.md` §10） | 只按 module 精确放行 `asyncpg` / `imapclient` 两个（理由已在 `docs/research/03-dependencies-and-toolchain.md` 登记），不放宽任何其他开关；放行与第一个真正 import 该库的模块同一次改动落地 |

### 8.2 已如实登记的代价（不缓解，只跟踪）

下列都是**已经接受**的代价，**不设缓解措施**——设了就等于偷偷改结论。完整的论证在各处原文，本节只做汇总指针，**不重述理由**。

| 登记的代价 | 原文出处 |
|---|---|
| 调用已完成、结果未提交时崩溃会**重复投递一次**（SMTP 可能其实已成功） | `design.md` §3 的「代价如实登记」 |
| 第 4 次尝试后崩溃的遗留行由收割支推进终态，**最终结果可能被记为失败而 SMTP 其实已成功** | `design.md` §3、§5.3 |
| 退信到达时刻不入库，**无法统计退信延迟** | `design.md` §3.3 |
| 退信取不出原邮件标识时被丢弃，**调用方看不到这次失败**；退信检测是尽力而为 | `design.md` §3.4、`spec.md` §7 |
| 「不点回复而新写一封」的答复**永不被收录**（回路只承认点回复） | `spec.md` §7、`design.md` §3.4 |
| 极老的邮件可能因一条卡住的待审行而**延后清理**（配合收割支，基本不兑现） | `design.md` §3.6 |
| 游标初始化或 `UIDVALIDITY` 轮换那一瞬间**已在信箱里的邮件不会被回收** | `design.md` §3.4 |
| 正常 tick（连接成功、拉回 0 封、无丢弃）**不留任何记录**，「用户没点回复」与「游标停摆但连接正常」仍不可区分 | `design.md` §3.4 的「已登记的残留盲区」 |
| 进程内 asyncio **没有集中式任务可视化与执行历史** | `docs/research/02-runtime-and-scheduling.md` |
| 多实例下 10 秒轮询会重复消费同一条记录（当前单实例部署，抢占逻辑仍按并发安全写） | `docs/research/02-runtime-and-scheduling.md` |
| 单进程并发上限受 GIL 与事件循环影响（本场景负载不构成瓶颈） | `docs/research/02-runtime-and-scheduling.md` |
| `From` 与被拉取信箱的一致性靠**启动即校验**维持：配置写错时服务直接起不来 | `design.md` §4.4 |
| 多一个必填配置项（`REPLY_TO_ADDRESS`）与三条启动判据 | `design.md` §4.4 |
| 仓储层只能用 SQLAlchemy 表达式构造器，**不能用 `text()` 裸串**；确实表达不出时**停下来提出**，不得绕过 | §2.9 的「兜底纪律」 |
| 基础镜像锁 codename tag 后，**安全更新不会自动进入**，需人工定期评估并重建 | `docs/research/01-delivery-and-deploy.md` |
| 生产宿主 CentOS 8 已 EOL、宿主 Docker 无官方支持 | `docs/research/01-delivery-and-deploy.md` 的「已知风险」 |

## 9. 分步实施顺序

### 9.1 步骤清单（与 `tasks.md` 的阶段对应）

阶段划分只有一条依据：**每一步都能被一条命令独立判定通过**。阶段名与 `tasks.md` 的五个阶段一一对应，编号 `S-n` 只是本节的引用把手，**任务编号用 `tasks.md` 的 `T-n`**。

**任务粒度：一条任务 = 一个垂直片。** 能在实现之前先红的单测（`core/` 四个模块、`schemas/`、仓储语句编译、请求校验、启动校验、配置口径、空实现标注、调度间隔）**与其对应的实现合并为同一条任务**，不另立“测试任务”——这样 `T-1` ~ `T-45` 的总数与下面的阶段区间保持不变，且每个任务天然自带它的验证方式（依据见 `docs/testing.md` §5「优先 TDD」）。集成测试与跨层静态检查留在阶段 5，因为它们要等迁移、夹具、真实库与五条流全部到位才写得出。

| 阶段（= `tasks.md` 的阶段） | 步骤 | 内容 | 对应任务 |
|---|---|---|---|
| **阶段 1 数据模型与迁移** | `S-1` | 四张表的 ORM 模型、引擎与会话、迁移文件（四张表 + 六项索引 + 全部 `CHECK` + 一处自引用外键），`upgrade()` / `downgrade()` 都实现 | `T-1` ~ `T-5` |
| | `S-2` | `migrations/alembic.ini` / `env.py` / `script.py.mako` 与该目录 README 的就地改写 | `T-4`、`T-39` |
| **阶段 2 仓储层** | `S-3` | 六个仓储模块：三阶段认领、条件 UPDATE、收割支、入站幂等插入、keyset 分页、清理的 `NOT EXISTS` | `T-6` ~ `T-12` |
| **阶段 3 纯逻辑与外部能力接口** | `S-4` | `core/` 四个模块（`message_id` / `retry` / `states` / `inbound`） | `T-13` ~ `T-16` |
| | `S-5` | `providers/{review,smtp,imap}/` 的 `base.py` 与两个实现 + 工厂 | `T-17` ~ `T-21` |
| **阶段 4 五条流与同步路径** | `S-6` | 五条流与调度器（`flows/`） | `T-22` ~ `T-26` |
| | `S-7` | `schemas/`、`service/`、`api/v1/`、`validate_startup()` 与 `main.py` 的 lifespan 装配 | `T-27` ~ `T-32` |
| **阶段 5 集成测试、文档与交付** | `S-8` | 集成测试与跨层静态检查（`AC-1`…`AC-41` 全覆盖，口径见 §7.3 / §7.4）；**随实现先红的单测不在这里重复** | `T-33` ~ `T-38` |
| | `S-9` | 文档同步与包占位 docstring（§6.2 的清单） | `T-39`、`T-40` |
| | `S-10` | 跑门禁、按 `AC-n` 回填 `spec.md` §9 的验收结果 | `T-41`、`T-42` |
| | `S-11` | 交付三件脚本与 `deploy/` / 研究册 / 未定项的同步 | `T-43`、`T-44` |
| | `S-12` | **交付阶段验证（不入门禁）**：`AC-20` 在隔离服务器上按步骤验证 | `T-45` |

**依赖顺序（不可跳序的理由）**：迁移（S-1）先于仓储（S-3），因为仓储的语句要在真实表上编译与执行；仓储先于流（S-6），因为流的每个阶段都调它；`core/`（S-4）与 `providers/` 接口（S-5）先于流，因为它们是被编排的对象；`service/` 与 `api/`（S-7）依赖仓储与 `core/`，但不依赖流；**文档与包占位（S-9）必须与实现同批落地**——漏改会让后来者按「未定案」去 `open-questions.md` 找一个已经不在那里的条目（§2.10）。

### 9.2 每步的独立验证方式

每一步都能单独验，**不靠「最后一起跑」**。下表的命令均可直接执行（集成测试的前置见 §7.2）。

| 步骤 | 独立验证方式 |
|---|---|
| `S-1` | `uv run alembic -c migrations/alembic.ini upgrade head` → 对着**空库**跑通；`uv run alembic -c migrations/alembic.ini downgrade base` → 四张表干净删除、无残留类型或索引；`uv run alembic -c migrations/alembic.ini upgrade head` 再跑一次仍成功。另：`tests/unit/test_constants_consistency.py` 断言迁移里的 `CHECK` 取值集合与 `constants.py`、契约三方一致 |
| `S-2` | 同上一条命令即可验证 `env.py` 的异步外壳（`alembic -c migrations/alembic.ini upgrade head` 走异步引擎而不需要额外配置）；`migrations/README.md` 的三处改写用只读检查确认：文件里不再出现「保留 Alembic 自动生成的 revision 哈希前缀」，且迁移入口写的是 `deploy/migrate.sh` |
| `S-3` | `uv run pytest tests/integration/test_repositories.py`：两个并发任务抢同一批只成功一次（`AC-29`）、唯一键挡住重复插入（`AC-11`）、条件 UPDATE 的 `rowcount = 0` 不报错、清理的 `NOT EXISTS` 不留下孤儿（`AC-22`）、250 条 keyset 翻页全集一致（`AC-41`）；纯语句层用 `tests/unit/test_repository_statements.py` 断言编译产物含 `FOR UPDATE SKIP LOCKED`（`AC-26`） |
| `S-4` | `uv run pytest tests/unit/test_message_id.py tests/unit/test_retry_policy.py tests/unit/test_states.py tests/unit/test_inbound_sorting.py`：四份纯逻辑单测全绿即通过。**这一步不碰数据库**，因此可以完全脱离 §7.2 的前置独立跑 |
| `S-5` | `uv run pytest tests/unit/test_review_placeholder.py`（`AC-25` 的关键词断言）+ `uv run mypy src`：三个 `base.py` 只依赖各自接口、不 import `db/`；`providers/review/` 的两个文件开头注释含「空实现」「永远通过」 |
| `S-6` | 单条流各自可验：`uv run pytest tests/integration/test_review_outbound_flow.py`、`test_send_flow.py`、`test_fetch_inbound_flow.py`、`test_review_inbound_flow.py`、`test_cleanup_flow.py`。每条流都是「构造行 → `await` 一次 tick → 断言状态与列值」，不依赖别的流先跑（跨流用例把需要的 tick 都显式调用一次）。调度器那一条随实现先红：`uv run pytest tests/unit/test_scheduler_intervals.py`（`AC-23`） |
| `S-7` | `uv run pytest tests/integration/test_api_contract.py tests/integration/test_startup_validation.py tests/unit/test_request_validation.py tests/unit/test_settings_validation.py tests/unit/test_config_hygiene.py tests/unit/test_schemas.py tests/unit/test_openapi_contract.py`：三个端点、422 清单、`found` 区分、时间格式、三条启动校验与配置口径各自独立可判定（`AC-1` ~ `AC-5`、`AC-15` ~ `AC-18`、`AC-27`、`AC-31`、`AC-32`、`AC-33`、`AC-35`、`AC-36`） |
| `S-8` | 每个测试文件单独可跑（上表逐个给了路径）；`AC-20` 除外，它在 `S-12` 按步骤验。**未跑真实数据库的用例不得计入通过**——`pytest` 的退出码就是判据 |
| `S-9` | 只读检查：`git grep -n "尚未定案" src/` 只剩 `providers/review/__init__.py` 里那半句「真实审查能力何时补上仍未定」；`docs/README.md` §3 的清单里已无 `configuration.md`；`tests/integration/README.md` 不再出现旧状态词「已排队」 |
| `S-10` | `bash scripts/check.sh` 五步全绿（`ruff format --check` → `ruff check` → `mypy src` → `pytest`（带覆盖率）→ `mutmut`）；随后把每条 `AC-n` 的结果回填 `spec.md` §9 |
| `S-11` | `bash -n deploy/migrate.sh deploy/run.sh`（语法自检）+ `uv run python deploy/check_archive.py --help`（用法与退出码 `0` / `1` / `2` 可判）；`bash deploy/migrate.sh` 在有版本的测试库上跑通，在无版本时打印「失败属预期」这条判别法。**产出方那一侧的脚本不写、不验**（随 D13 推迟） |
| `S-12` | 在隔离服务器上：`bash deploy/run.sh` → 轮询 `/healthz`；`curl -fsS http://<host>:<port>/healthz` 60 秒内返回 200；另按 `docs/research/01-delivery-and-deploy.md` 的方法性结论逐条核对（`docker load` 后逐 tag `inspect` 架构、归档自检在生成 `.sha256` **之前**执行） |

**门禁与验收的分界**：`S-1` ~ `S-11` 全部在本地门禁内（`pytest` 覆盖 `AC-1` ~ `AC-19`、`AC-21` ~ `AC-41`）；`S-12` 是**交付阶段**的验收动作，结果同样回填 `spec.md` §9，但不加进 `pytest`。

## 10. 回滚方式

### 10.1 迁移回滚

**回滚动作 = `alembic downgrade`，必须是可执行的一等公民，不是「理论上能退」。**

| 项 | 口径 |
|---|---|
| 回滚入口 | 容器内 `docker compose run --rm app uv run alembic -c migrations/alembic.ini downgrade base`；迁移入口是 `deploy/migrate.sh`，回滚沿用同一条命令形状。**不在宿主机手工跑 Alembic**（`migrations/README.md` §约定 6） |
| 本特性的回滚面 | 首版迁移 `0001_create_mail_tables.py` 的 `downgrade()` 按依赖倒序删四张表：先删 `messages`（它带自引用外键），再 `pending_messages`、`rejected_messages`、`imap_cursors`。**必须实现**，且要连着索引与 `CHECK` 一起干净移除 |
| 是否有不可逆步骤 | **没有**。首版迁移只做建表，不删列、不改列类型、不做数据搬运，因此 `downgrade()` 不需要保留任何数据——**没有写不出 `downgrade()` 的迁移**（`migrations/README.md` §约定 2 的例外在本特性不触发） |
| 回滚会丢什么 | 回滚就是**丢掉四张表里的全部数据**（邮件、回复、拒审记录、IMAP 游标）。一期尚未投产，这条没有实际代价；**投产之后**回滚等于丢弃 30 天内的邮件与回复，属破坏性动作，要先按 `docs/research/01-delivery-and-deploy.md` 的「维护与回滚」走备份（停机打包数据目录，热备用 `pg_dump`） |
| 回滚后的行为 | 表不存在时应用会连不上库并在查询时报错——**这不是静默降级**。回滚镜像 tag（`VS_EMAIL_FORWARDING_IMAGE` 改成旧 tag 再 `up -d`）与回滚迁移**必须成对做**：如果新版本才引入这四张表，只回滚镜像、不回滚迁移会把新表留给旧版本（旧版本不认识它们，但也不会报错），反之只回滚迁移会让新版本在运行中直接报错。`docs/research/01-delivery-and-deploy.md` 已定「若新版本含破坏性迁移，需单独执行 `alembic downgrade`，不能只回滚镜像」 |
| 验证方式 | `uv run alembic -c migrations/alembic.ini upgrade head` → `uv run alembic -c migrations/alembic.ini downgrade base` → `uv run alembic -c migrations/alembic.ini upgrade head` 三步连续跑通，且第二次 upgrade 后库里仍是四张表、六项索引与全部 `CHECK`（§9.2 的 `S-1`） |

### 10.2 数据与旧版本兼容

**本特性是首个迁移，不存在「旧版本读新数据」这一类问题**，逐条说明：

- **没有历史数据需要迁移**：库里此前不存在任何业务表——`src/vs_email_forwarding/db/` 在此之前只有包占位文件，`migrations/` 在此之前只有一份职责 README（§3.3）。因此本次是**纯新增表结构**：没有字段改名、没有默认值回填、没有数据搬运。
- **没有旧版本读写过这些表**：本服务尚未发布过任何读写邮件数据的版本，「旧版本读新数据」与「新版本读旧数据」两个方向都无对象。
- **兼容性的唯一实际风险是「代码与迁移不同步」**：镜像里带新代码、库里还是旧迁移（或反过来）会让应用在运行时报错。缓解是部署顺序固定为「先 `deploy/migrate.sh` 迁移、再 `docker compose up -d`」，且迁移是幂等的（`upgrade head` 重复执行无副作用）。
- **首次迁移在无版本时失败是预期行为**：`migrations/` 里还没有版本时执行 `alembic -c migrations/alembic.ini upgrade head` **必然失败**，`deploy/migrate.sh` 必须把这条判别法打印出来（`migrations/README.md` §约定 6 与 `docs/research/01-delivery-and-deploy.md` 的方法性结论），避免运维把它当成故障。
- **前向兼容的边界留给后续特性**：新端点一旦被调用方按固定契约实现，后续任何字段增删或六态语义变更都是破坏性变更，而一期没有版本协商机制（§4.3）。真要做，属新的决策，按 `AGENTS.md` §6 先改写 `design.md` §4.1 与 `docs/api-contract.yaml`。
