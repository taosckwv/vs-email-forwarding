# 设计文档（内部邮件中转服务 · `specs/0001`）
> 本文件是本特性设计结论的唯一真源：数据模型、流水线细节、集成契约与依赖选型的结论都在这里。
> 需求在 `spec.md`；跨特性底座（调度载体与并发抢占硬约束、技术栈与门禁、安全红线、交付与环境、命名与治理）在 `docs/research/` 各册，本文件只在需要时给指针，不复述。
> 写作规范见 `docs/README.md` §4：只写当前有效内容，不写时间线与决策过程。

## 1. 概览
调用方（内网 agent 会话）用 `POST /v1/messages` 把一封要发给某位内部同事的邮件交给本服务；服务先做安全审查，审查通过后经 SMTP 投递出去；同事回复之后，服务从专用回复邮箱用 IMAP 把回复拉回来，调用方再按原邮件 ID 查询这些回复。调用方不接触 SMTP / IMAP，只与 HTTP API 打交道。

```text
 调用方            服务后台（四条业务流）                 库 / 外部
 ───────────────────────────────────────────────────────────────
 POST /v1/messages ──写待审行──▶ [待审表 outbound]
                                    │ 流① 审查
                       ┌────────────┴────────────┐
                    通过                      不通过 / 出错
                       ▼                          ▼
              [邮件表 sending] ─流② 发信─▶ SMTP   [拒审表]
                                            │
                      收件人回复 ──▶ 专用回复邮箱
                                            │
 流③ 拉取 ◀─────────────────────────────────┘
   ├─ 关联上 ──▶ [待审表 inbound] ─流④ 审查─┬─ 通过 ──▶ [邮件表 received]
   │                                        └─ 不通过 / 出错 ──▶ [拒审表]
   ├─ 退信 ────▶ [邮件表 sent → failed]
   └─ 其余 ────▶ 丢弃（只记条数）

 GET /v1/messages/{id}/replies ──查邮件表──▶ 调用方取回回复
```

图里每一行只沿一条流前进：四条业务流各自的状态去向以此为准，每一步落在哪张表也标在图上。每条流自己的认领条件与事务边界见 §3；`imap_cursors` 不承载邮件行，故不在图上。

保留期清理是第 5 条后台任务，它不参与邮件行的流转，故不入图；同一张物理待审表在图里出现两次（出站段与入站段各一次）是为了看清流向，实际是同一张表。

### 本设计的不变量
| 不变量 | 细节在哪 |
|---|---|
| 库中任何时刻都不存在需要回收的中间态 | §2.3 状态机 |
| 数据库事务不跨越网络调用 | §3 流水线（认领与事务边界） |
| 入站幂等由「游标 + 唯一约束」共同保证，不以 `Message-ID` 作判据 | §2.2 约束与索引、§3.4 流③ |
| 错误列不落任何自由文本 | §2.4 重试与错误列 |
| 退信只作用于已发出的行（`sent_at` 非空），不打断重试 | §3.3 流② |
| 第 4 次尝试后崩溃的遗留行由「收割」推进到终态，不产生第 5 次尝试 | §3 流水线 |
| 待审表是纯队列 | §2.1 表与字段清单、§3.2 流① |
| 清理只覆盖邮件表与拒审表，回复由外键级联删除 | §3.6 保留期清理 |
| 对外邮件 ID 就是库中主键，不另设一套对外标识 | §2 数据模型 |
| 发件人 `From` 固定取自配置，服务端显式写入 `Reply-To` | §4.3 发件人身份与回复路由 |
| 被拉取信箱地址必须等于 `REPLY_TO_ADDRESS`，不一致则拒绝启动 | §4.4 启动即校验 |

## 2. 数据模型
全库物理表 **4 张**：邮件数据表 3 张（待审表 = `pending_messages`、邮件表 = `messages`、拒审表 = `rejected_messages`）加调度状态表 1 张（游标表 = `imap_cursors`）。待审表由出站与入站共用，用 `direction` 区分方向；审查不通过的记录移入独立的拒审表。
四张表的中文名与物理表名一一对应；下文凡涉及迁移、ORM、表结构、索引、约束与落库一律用物理表名，正文叙述与 §1 的图仍用中文名。

三张邮件数据表的主键统一为应用生成的 UUIDv4（PostgreSQL 原生 `uuid` 列），该值就是 API 契约里的 `{id}`，不另设一套对外标识。不用数据库自增 `bigint`：一期不做 API 鉴权（见 `docs/research/04-security-and-review.md`），可枚举的 ID 会让内网任意主机顺序遍历读走他人的邮件状态与回复内容。

```text
 pending_messages ──过审 / 拒审后删除自身，id 与目标行同值
      ├──▶ messages（出站行）
      └──▶ rejected_messages（只存结构信息）

 messages.parent_id ──自引用外键──▶ messages（入站回复行）
     出站行的该列为空；删除出站行时级联删除它的回复行

 imap_cursors：独立表，不与其他表互引
 四张表之间只有这一处外键（parent_id）；「跨表 ID 同值」指 `id`，不是外键
```

- 邮件表用单表承载两类行——出站邮件与入站回复，用 `direction` 区分；回复行用自引用外键指向被回复的那条出站行（出站行的该列为空），`NFR-8` 的「邮件与它的回复一起移除」由该外键的级联删除保证，不靠应用层记得写两条删除，`GET /v1/messages/{id}/replies` 因此退化为单表单索引查询。
- 入站分拣时，先用 `message_id` 的唯一索引把 `In-Reply-To` / `References` 里的标识解析成本表出站行的主键，再写入回复行；`message_id` 只装服务自己生成的出站 `Message-ID`，出站行必填并建全局唯一索引，入站回复行该列为 NULL。
- 回复行自身的 `Message-ID` 头值不入库，它由收件人侧客户端生成、属外部可控数据，既不参与关联也不参与幂等，进唯一列只会多出一条「外界能让我们写库失败」的路径；原始的 `In-Reply-To` / `References` 串同样不入库，解析结果已经落成 `parent_id` 外键，再留一份原始串只会造成「该信外键还是信字符串」的第二个真相来源。
- 三个容易混淆的「信箱」概念：**被拉取信箱地址**是拿去与 `REPLY_TO_ADDRESS` 比对的那个值（见 §4.4）；**信箱文件夹**是 IMAP 从哪个文件夹拉（配置项 `IMAP_MAILBOX`）；表上的 `mailbox` 列取值也来自**信箱文件夹**（见 §2.1）。

### 2.1 表与字段清单
**待审表**（`pending_messages`）——在审邮件的唯一落点，不设 `status` 列：表里只剩「等待审查」一种行，认领条件就是 `next_attempt_at <= now()`；状态由「这一行在哪张表里」承担，不另设第二个真相来源。

| 字段 | 类型或取值 | 说明 |
|---|---|---|
| `id` | `uuid` | 与目标行（`messages` 或 `rejected_messages`）同值 |
| `direction` | 文本 + `CHECK`：`outbound` / `inbound` | 区分出站邮件与入站回复；`pending_messages` 与 `messages` 都靠它区分这两类行 |
| `message_id` | 文本 | 仅出站行：服务生成并持久化的出站 `Message-ID`；入站回复行该列为 NULL |
| `recipient` | 文本，NOT NULL | 收件人地址；库中只存单个地址，接口层按 `AC-2` / `AC-3` 做 422 校验 |
| `subject` | 文本，NOT NULL | 邮件主题，提交时由调用方给定；长度上限见 §2.6 |
| `body` | 文本，NOT NULL | 邮件正文，提交时由调用方给定；长度上限见 §2.6 |
| `body_format` | 文本 + `CHECK`：`text` / `html`，默认 `text` | 正文格式，决定按纯文本还是 HTML 投递；非法格式写不进库 |
| `parent_id` | `uuid` | 仅入站行：指向被回复的那条出站行 |
| `mailbox` | 文本 | 仅入站行：拉回该回复的信箱文件夹，一期恒为配置值 `IMAP_MAILBOX` |
| `uidvalidity` | 整型 | 仅入站行：该信箱的 `UIDVALIDITY`，入站幂等键之一 |
| `uid` | 整型 | 仅入站行：该邮件在信箱内的 UID，入站幂等键之一 |
| `attempt_count` | 整型 | 该行已被认领处理的次数；`FR-11` 的「审查中」即「待审行且 `attempt_count > 0`」 |
| `next_attempt_at` | `timestamptz` | 下次可处理时刻；建行即等于当前时刻，认领时被推到 `now + backoff[n]`（§3 阶段①） |
| `last_error_class` | 文本 + `CHECK`：`permanent` / `transient` / `internal`，可为空 | 最后一次错误的受控分类，取值含义见下面的枚举表 |
| `created_at` | `timestamptz`，**NOT NULL**（不加 DDL 默认值） | **本行产生时刻**：出站行 = 调用方提交时刻，入站行 = 该回复落库时刻；`NFR-8` 的 30 天保留期自它起算，因此不可为空（见 §2.5） |

**邮件表**（`messages`）——审查通过后的出站邮件与已入库的入站回复：

| 字段 | 类型或取值 | 说明 |
|---|---|---|
| `id` | `uuid` | 与待审行同值 |
| `direction` | 文本 + `CHECK`：`outbound` / `inbound` | 区分出站邮件与入站回复 |
| `status` | 文本 + `CHECK`：`sending` / `sent` / `failed` / `received` | 该行的状态取值；合法迁移见 §2.3，取值含义见下面的枚举表 |
| `message_id` | 文本 | 仅出站行：服务生成的出站 `Message-ID`，全局唯一；入站回复行该列为 NULL |
| `parent_id` | `uuid` | 仅入站行：自引用外键指向被回复的出站行，级联删除 |
| `recipient` | 文本，NOT NULL | 收件人地址；库中只存单个地址 |
| `subject` | 文本，NOT NULL | 邮件主题，提交时由调用方给定；长度上限见 §2.6 |
| `body` | 文本，NOT NULL | 邮件正文，提交时由调用方给定；长度上限见 §2.6 |
| `body_format` | 文本 + `CHECK`：`text` / `html`，默认 `text` | 正文格式，决定按纯文本还是 HTML 投递 |
| `mailbox` | 文本 | 仅入站行：拉回该回复的信箱文件夹，入站幂等键之一 |
| `uidvalidity` | 整型 | 仅入站行：该信箱的 `UIDVALIDITY`，入站幂等键之一 |
| `uid` | 整型 | 仅入站行：该邮件在信箱内的 UID，入站幂等键之一 |
| `attempt_count` | 整型 | 该行已被认领处理的次数；出站行以它累计发信尝试。**跨表搬运到本表时重置为 0**（审查重试不侵占发信重试额度，见 §2.4） |
| `next_attempt_at` | `timestamptz` | 下次可处理时刻；退避在认领时写在这里（§3 阶段①）。**跨表搬运到本表时重置为 `now()`** |
| `last_error_class` | 文本 + `CHECK`：`permanent` / `transient` / `internal`，可为空 | 最后一次错误的受控分类，取值含义见下面的枚举表 |
| `last_smtp_code` | 整型，可为空 | 最后一次 SMTP 返回码；审查环节没有 SMTP 返回码，该列只在 `messages` 上有意义 |
| `created_at` | `timestamptz`，**NOT NULL**（不加 DDL 默认值） | **本行产生时刻**：出站行 = 原始提交时刻，跨表搬运时不得重新取 `now()` 覆盖；入站回复行 = 该回复落库时刻；保留期判据不可为空（见 §2.5） |
| `sent_at` | `timestamptz` | 投递被接受的时刻；退信到达后保留不清空（见 §3.3） |
| `received_at` | `timestamptz` | 回复入库时刻（UTC），也是回复的排序键 |
| `reviewed_at` | `timestamptz` | 审查结束时刻，保留「审查耗时」这一最小审计信息 |

**拒审表**（`rejected_messages`）——审查未通过的记录，只存结构信息：

| 字段 | 类型或取值 | 说明 |
|---|---|---|
| `id` | `uuid` | 与提交时的对外 ID 同值 |
| `direction` | 文本 + `CHECK`：`outbound` / `inbound` | 区分被拒的是出站邮件还是入站回复 |
| `source` | 文本 + `CHECK`：`review_rejected` / `review_error` / `review_incomplete` | 拒审来源（`review_rejected` = 审查判定不通过，`review_error` = 审查环节出错，`review_incomplete` = 第 4 次尝试被认领后崩溃、由收割支推进终态）；仅供库内复核与运维排查，不对外暴露 |
| `subject` | 文本，可为空 | 仅入站行：被拒回复的主题（出站行必须为 NULL） |
| `body` | 文本，可为空 | 仅入站行：被拒回复的正文（出站行必须为 NULL） |
| `last_error_class` | 文本 + `CHECK`：`permanent` / `transient` / `internal`，可为空 | 最后一次错误的受控分类，取值含义见下面的枚举表 |
| `created_at` | `timestamptz`，**NOT NULL**（不加 DDL 默认值） | **本行产生时刻**：出站拒审 = 原始提交时刻；入站拒审 = 该回复落库时刻；保留期判据不可为空（§2.5） |
| `reviewed_at` | `timestamptz` | 审查结束时刻 |

`rejected_messages` 对**出站**拒审不保留邮件原文（收件人、主题、正文、格式都不存）：复核误判所需的原文本来就在调用方手里——就是它提交的那一份；服务侧再存一份的收益是重复的，代价却是把全库敏感度最高的一份内容按 `NFR-8` 留存 30 天。**入站回复是例外**：它的原文来自对方，调用方手里没有，拒审后若不存就**任何地方都不存在**、复核误判无从谈起——因此入站拒审行保留**主题与正文**两项最小可复核内容（仍不存原始 MIME、不存回复者其他头值），并由 `CHECK` 保证只有入站行非空。

**游标表**（`imap_cursors`）——IMAP 拉取进度，每个信箱文件夹一行：

| 字段 | 类型或取值 | 说明 |
|---|---|---|
| `mailbox` | 文本，唯一 | 信箱文件夹（不是被拉取信箱地址）；一期只会有 1 行 |
| `uidvalidity` | 整型 | 该信箱的 `UIDVALIDITY`；一变就重置 `last_uid` |
| `last_uid` | 整型 | 已处理的最大 UID，游标只负责「下次从哪开始拉」 |
| `updated_at` | `timestamptz` | 该行最后一次写入的时刻 |

独立成表：「`UIDVALIDITY` 一变就重置 `last_uid`」这条规则因此在表结构上一眼可见；拉取热路径上只是一次主键查询；天然支持多邮箱。游标只负责「下次从哪开始拉」的效率；同一 `UIDVALIDITY` 内的正确性由 `mailbox + uidvalidity + uid` 的唯一约束保证，`UIDVALIDITY` 轮换场景由游标的 `UIDNEXT - 1` 规则补平（§3.4）。

#### 受控枚举取值表
| 枚举 | 取值 | 含义 | 谁在用 |
|---|---|---|---|
| `direction` | `outbound` | 出站邮件：调用方提交、经本服务发给收件人的邮件 | `pending_messages`、`messages`、`rejected_messages` |
| `direction` | `inbound` | 入站回复：从专用回复邮箱拉回、关联回某封出站邮件的用户回复 | `pending_messages`、`messages`、`rejected_messages` |
| `status` | `submitted` | 已提交：待审行且 `attempt_count = 0`（尚未被认领过） | `pending_messages` |
| `status` | `reviewing` | 审查中：待审行且 `attempt_count > 0`（含审查失败的退避等待） | `pending_messages` |
| `status` | `rejected` | 拒审：审查判定不通过或审查环节出错用尽重试 | `rejected_messages` |
| `messages.status` | `sending` | 投递中：出站行的初始态，同时覆盖「首次待发」与「重试等待」 | `messages` 出站行 |
| `messages.status` | `sent` | 投递已被接受 | `messages` 出站行 |
| `messages.status` | `failed` | 投递终态失败；退信到达后也从 `sent` 置为 `failed` | `messages` 出站行 |
| `messages.status` | `received` | 入站回复行，写入即终态 | `messages` 入站行 |
| `body_format` | `text` | 纯文本正文（默认值） | `pending_messages`、`messages`、审查接口 |
| `body_format` | `html` | HTML 正文 | `pending_messages`、`messages`、审查接口 |
| `last_error_class` | `permanent` | 永久失败：`5xx`，直接置终态 `failed`，不再消耗剩余重试次数 | `pending_messages`、`messages`、`rejected_messages` |
| `last_error_class` | `transient` | 临时失败：`4xx` 与连接 / 超时类故障，按固定重试语义重试 | `pending_messages`、`messages`、`rejected_messages` |
| `last_error_class` | `internal` | 服务自身内部故障（审查环节异常、代码或组装错误这一类），兼表「对端退信但 `Status` 不可分类」（裁决记录见 §3.3「退信到达」）；重试处理与 `transient` 一致，区别只在分类语义 | `pending_messages`、`messages`、`rejected_messages` |
| `rejected_messages.source` | `review_rejected` | 审查器返回 `reject`：不投递（出站）/ 不入库（入站），直接写本表 | `rejected_messages` |
| `rejected_messages.source` | `review_error` | 审查器抛异常，按固定重试语义重试，用尽后同样写本表 | `rejected_messages` |
| `rejected_messages.source` | `review_incomplete` | 第 4 次尝试已被认领、结果未提交（崩溃窗口）的行由收割支推进终态 | `rejected_messages` |

这些取值在库里以文本存储，由 `CHECK` 约束保证合法，不使用 PostgreSQL 原生 `ENUM`。

**状态取值只有这一套，库内与对外完全同名同义**（`submitted` / `reviewing` / `rejected` / `sending` / `sent` / `failed`），不存在「库内四态 → 对外六态」的映射层；`received` 是入站回复行的终态，不属于出站状态机。**六态按物理落点拆分存储**，`status` 列不集中在一张表上：

| 状态 | 物理落点与判据 |
|---|---|
| `submitted` | `pending_messages` 有该行且 `attempt_count = 0` |
| `reviewing` | `pending_messages` 有该行且 `attempt_count > 0`（含审查失败的退避等待期间） |
| `rejected` | `rejected_messages` 有该 ID 的行 |
| `sending` / `sent` / `failed` | `messages` 出站行的 `status` 列取值 |

拆成三张表的理由是隐私与队列语义，不是状态语义：`pending_messages` 过审即删行（纯队列，不随邮件量增长）、`rejected_messages` 对**出站**拒审不保留主题与正文（`§2.1`）。把六态塞进同一张表就必须让拒审行跳过内容列的 `NOT NULL`，那正是拆表要避免的。

### 2.2 约束与索引
| 约束 / 索引 | 落点 | 作用 |
|---|---|---|
| `message_id` 全局唯一索引 | `messages` | 出站行必填；入站回复关联的解析依据 |
| `(mailbox, uidvalidity, uid)` 部分唯一索引，`WHERE direction = 'inbound'` | `pending_messages` 与 `messages` 各一份 | 同一 `UIDVALIDITY` 内的入站幂等，`AC-11` 由数据库唯一约束兜底 |
| 认领索引 `(status, next_attempt_at)` | `messages` | `SELECT ... FOR UPDATE SKIP LOCKED` 抢占 |
| 认领索引 `(direction, next_attempt_at)`，部分索引 `WHERE attempt_count <= 4` | `pending_messages` | 流① / 流④ 的抢占；首版即建，不等积压出现（§6「事务与认领」） |
| 回复查询索引 `(parent_id, received_at)` | `messages` | `GET /v1/messages/{id}/replies` |
| 清理索引 `(created_at)` | `messages`、`rejected_messages` | 保留期清理（§3.6） |
| 自引用外键 `parent_id` + 级联删除 | `messages` | `NFR-8` 的「邮件与它的回复一起移除」 |
| `CHECK`：每个受控枚举列的取值集合（`direction`、`status`、`body_format`、`last_error_class`、`source`） | `pending_messages`、`messages`、`rejected_messages` | 受控枚举一律以文本列存储，取值合法性由 `CHECK` 兜底，不用原生 `ENUM` |
| `CHECK`：`direction` 与 `status` 的合法组合 | `messages` | 入站行只能是 `received`，出站行不得为 `received` |
| `CHECK`：出站行必须有 `message_id`，入站行必须有 `mailbox` / `uidvalidity` / `uid` | `messages` | 把行模型的必填关系钉在库里 |
| `CHECK`：出站行不得有 `mailbox` / `uidvalidity` / `uid` / `parent_id`，入站行不得有 `message_id` | `messages` | 反向禁止，堵住混装行的 NULL 语义漏洞 |
| `CHECK`：`subject` / `body` 仅入站行非空、出站行必须为 NULL | `rejected_messages` | 拒审表只对入站回复保留可复核内容 |
| `mailbox` 唯一 | `imap_cursors` | 每个信箱文件夹一行 |

- 入站幂等约束**`pending_messages` 与 `messages` 都要建**：只建在 `pending_messages` 上时，过审删行之后游标一旦回退就会重新插入待审行、白走一遍审查，直到写 `messages` 才撞唯一键；两处都有约束，重复的入站邮件在落 `pending_messages` 时即被拒。
- 入站回复行上直接带 `mailbox` / `uidvalidity` / `uid` 三列，判重与插入因此是同一次插入、天然原子：宕机重跑、游标回退或并发处理都不会产生第二条记录，与「游标管效率、唯一键管同一 `UIDVALIDITY` 内的正确性」一致。
- **`pending_messages` 首版就建认领索引**（`(direction, next_attempt_at)`，部分索引 `WHERE attempt_count <= 4`）：它只承载在审队列、过审即删行，但「积压出现的时刻」正是系统最忙的时刻，那时再加索引等于一次 DDL 加一次迁移；首版即建只多一条索引的写入成本（原「不建」的结论已被取代，见 §6「事务与认领」）。

### 2.3 状态机
```text
 submitted ──开始审查──▶ reviewing ──过审──▶ sending ──投递被接受──▶ sent ──退信到达──▶ failed
      │                      │                  │                                        ▲
      │                      └──判定拒 / 出错──▶ rejected                            │
      │                                         └──投递终态失败 / 重试用尽────────────┘

 received：入站回复行，写入即终态，不迁移
```

出站行的合法迁移只有 `sending → sent`、`sending → failed`、`sent → failed`；入站行只有 `received`，不迁移。

**出站状态机就是这六态，库内与对外同一套取值，不存在映射层。**「投递中」（`sending`）是 `messages` 出站行的**真实状态值**，涵盖「已过审、首次待发」与「投递失败后的重试等待」两种情形；`submitted` / `reviewing` / `rejected` 的物理落点见 §2.1 的落点表。状态因此**单调推进**（已提交 → 审查中 → 投递中 → 已发出 / 发送失败，拒审是从「审查中」分出的终局），调用方不必处理状态倒退。迁移合法性由**带谓词的条件 UPDATE** 保证（只允许声明的起点，`rowcount = 0` 按幂等忽略），不引入数据库触发器（见 §6「状态与重试」）。

- **`sending` 是显式等待态，不是「中间态」**：「中间态」指的是「已认领、结果未提交」那种外部不可观测且**需要超时回收**的状态，本设计仍然一个都没有。`sending` 相反：它是**已提交的等待事实**（等下一次认领），崩溃恢复照旧只看状态字段与 `next_attempt_at`，不需要任何回收机制；这也正是它必须落库、不能由 `next_attempt_at` 或 `sent_at IS NULL` 推断出来的原因——`sent_at IS NULL` 同时涵盖 `submitted` / `reviewing` 与 `failed`（从未被接受），推断不出「已过审」。
- `rejected_messages` 里的行表示审查判定不通过或审查环节出错，对外终局都是「拒审」，由 `source` 列区分来源。退避完全由 `attempt_count` 与 `next_attempt_at` 表达，不引入「发送中」「重试中」这类需要回收的中间态。
- 因为任何时刻都不存在需要回收的中间态，进程重启后的恢复只依赖状态字段与 `next_attempt_at`，不需要额外的超时回收机制。由此带来的「重复投递窗口」这一条代价，完整登记在 §3（认领与事务边界），此处不复述。

### 2.4 重试与错误列
`pending_messages` 与 `messages` 共用同一套重试列：`attempt_count`、`next_attempt_at`（建行即等于当前时刻，供抢占查询直接用）、`last_error_class`（文本 + `CHECK`：`permanent` / `transient` / `internal`，可为空）；`last_smtp_code`（整型，可为空）只在 `messages` 上有意义。
`attempt_count` 与 `next_attempt_at` 只在两处写：**认领事务**（认领第 n 次即 `attempt_count = n`、`next_attempt_at = now + backoff[n]`）与**目标行建行时**（跨表搬运到 `messages` 必须重置为 `0` 与 `now()`，否则审查重试用掉的次数会侵占发信重试额度）；结果事务一律不碰这两列（见 §3 阶段① / ③）。

**收割**：认领谓词放宽到 `attempt_count <= 4`，因此「第 4 次尝试已认领、结果事务未提交」的崩溃遗留行仍会被认领一次；这一次**不递增计数、不推退避、不发起外部调用**，直接进结果事务把它推进终态（`messages` → `failed`；`pending_messages` → `rejected_messages`，`source = review_incomplete`）。没有这一支，该行此后永远无人认领，对外永久停在「已提交」/「审查中」，而 `pending_messages` 不参与清理、卡住即永久。这一支也不产生真实的第 5 次投递。

- **错误里不落任何自由文本**：`FR-5` / `AC-8` / `AC-30` 要求的「只记 SMTP 返回码与永久 / 临时分类」由列类型保证，而不是靠写入方自觉；`permanent` / `transient` 就是 `AC-8` 要的分类，SMTP 返回码的分类策略落地时不需要改表。
- 三个取值的分工：`permanent` = 对端永久拒绝（`5xx`）；`transient` = 对端临时故障（`4xx` 与连接、超时）；`internal` = 服务自身内部故障（审查环节异常、代码或组装错误这一类），兼表「对端退信但 `Status` 不可分类」——这一支是对端行为、只是读不出永久 / 临时子类，复用 `internal` 而不另设第四类的裁决记录见 §3.3「退信到达」。
- `internal` 在重试处理上与 `transient` 一致（按固定重试语义重试，用尽后同样写 `rejected_messages`，见 §4.2 的「抛异常」一行），区别只在分类语义。

### 2.5 时间字段
| 字段 | 语义 | 用途 |
|---|---|---|
| `created_at` | **本行产生时刻**，**NOT NULL 且不加 DDL 默认值** | `NFR-8` 的 30 天保留期自此时起算。出站行 = 调用方提交时刻，跨表搬运时不得被重新取 `now()` 覆盖；**入站回复行 = 该回复落库时刻**（回复没有「调用方提交」这一事件；若沿用父行的提交时刻，出站提交后第 29 天到达的回复只剩 1 天保留期，违反 `NFR-8`） |
| `sent_at` | 投递被接受的时刻 | 区分「曾经被接受、事后被退信」与「从未被接受」（§3.3） |
| `received_at` | 回复入库时刻（UTC，写回复行时取） | 回复排序键 |
| `reviewed_at` | 审查结束时刻 | 最小审计信息 |
| `next_attempt_at` | 下次可处理时刻 | 抢占与退避 |

`created_at` 是这四张表里**唯一既 NOT NULL 又不加 DDL 默认值**的时间列，两个约束各治一种病：`NOT NULL` 保证 `NFR-8` 的 30 天保留期（§3.6 的清理判据就是它）对每一行都成立——可空会出现「NULL 的行永不被清理」这种语义不可解释、且违背 `NFR-8` 的行；不加 `now()` 默认值则保证 §3.1 / §3.2 的「跨表搬运不得重新取 `now()` 覆盖」在结构上无法被绕过——漏写这一列是**插入直接失败**，而不是静默把原始提交时刻换成搬运时刻。其余四个时间列（`sent_at` / `received_at` / `reviewed_at` / `updated_at`）表示「某个可能尚未发生的事件发生的时刻」，事件没发生就没有值，因此一律允许 NULL、同样不加 DDL 默认值，由应用在写行的同一事务里显式给出。

`messages` 不设 `updated_at`，也不存邮件 `Date` 头。回复的排序键取服务自己的入库时刻 `received_at`：入站按 UID 升序逐封处理，同批内 `received_at` 的先后即与邮箱内顺序一致；查询用 `received_at ASC, id ASC` 作确定性兜底，`AC-15` 要求的「顺序稳定」由结构保证。

### 2.6 内容字段与长度上限
`recipient`（单个地址）、`subject`、`body` 三列均为 NOT NULL 的文本，另有 `body_format` 受控枚举 `text` / `html`（默认 `text`）。非法格式写不进库；「只有一个收件人」由单值列在结构上表达，接口层再按 `AC-2` / `AC-3` 做 422 校验且不落库。

主题与正文的长度上限都是**配置项**：`MAX_SUBJECT_CHARS`（默认 500，单位就是字符）与 `MAX_BODY_BYTES`（默认 1048576 = 1 MiB，单位是字节、写进名字），由 API 层校验，超限返回 422 且不落库。
超限正文注定被 SMTP 拒绝，收下它只会走完审查、进库、4 次尝试全部失败，并在库里按 `NFR-8` 留 30 天；交付链是「服务器本地构建 + 基础镜像 tar 带入」，上限放进配置才能在撞到 MTA 限制时只改 `.env`。

## 3. 流水线
后台任务共五条：四条业务流（流① 审查出站、流② 发信、流③ 拉取入站、流④ 审查回复）加上保留期清理这一条。调度载体（进程内 asyncio、随 FastAPI lifespan 启停）见 `docs/research/02-runtime-and-scheduling.md`；一条邮件从 `POST` 到回复可查的全过程与各步落表见 §1 的图，下面每条流只写自己的认领 → 调用 → 落库。

认领与外部调用统一走三阶段，数据库事务不得跨越网络调用；`attempt_count` 与 `next_attempt_at` **只在阶段① 写**：

- ① 认领（短事务）：用 `SELECT ... FOR UPDATE SKIP LOCKED` 挑出**已到点且 `attempt_count <= 4`** 的行，分两支：
  - `attempt_count < 4`：认领第 n 次时写 `attempt_count = n`，并把 `next_attempt_at` 推为 `now + backoff[n]`（n=1 → 15s、n=2 → 30s、n=3 → 60s；n=4 不推，因为这一行只剩终态一条路），随后提交（`NFR-1`）。
  - `attempt_count = 4`（**收割**）：不递增、不推退避、**不发起外部调用**，直接进阶段③ 把它推进终态。它来自「第 4 次尝试已认领、结果事务未提交」的崩溃窗口；没有这一支，该行此后永远无人认领（`attempt_count < 4` 不再命中），对外永久停在「已提交」/「审查中」，`pending_messages` 还不参与清理、卡住即永久。
- ② 调用（事务外）：执行审查 / SMTP 投递（收割行跳过此步）。单次调用受 `SMTP_TIMEOUT_SECONDS` / `IMAP_TIMEOUT_SECONDS` / `REVIEW_TIMEOUT_SECONDS` 约束（默认各 10 秒，见 §7 配置项清单），`FR-15` 的 3 分钟由这几个上界与退避表共同推出。
- ③ 结果（第二个事务）：写目标行 + 删待审行，只写 `status` / `last_error_class` / `last_smtp_code` / `sent_at` / `received_at` / `reviewed_at`；目标行**建行时**把 `attempt_count` 与 `next_attempt_at` 重置为 `0` 与 `now()`（§2.4）。状态写入一律**带谓词的条件 UPDATE**（只允许 §2.3 声明的起点），`rowcount = 0` 或撞唯一键 / 主键按幂等忽略（视为「这一封已经处理过」），不报错、不重复写。
- 重试算术：「最多 3 次」指**最多 3 次重试**，即首次尝试 + 3 次重试 = 最多 4 次尝试，15s / 30s / 60s 三档退避全部用到。
- 结果口径：投递成功 → `sent` + `sent_at`；失败且 `attempt_count` 已达 4 → 置 `failed`（终态）；失败但未达上限 → `status` 保持 `sending` 不动，退避由阶段① 写好的 `next_attempt_at` 承担。
- `5xx` 是例外：直接置终态 `failed`，不等上限、不再消耗剩余重试（`AC-34` 的 `attempt_count` 为 1 由此成立）。
- 因此「进行中」由 `attempt_count` 与 `next_attempt_at` 表达，状态列的取值集合与对外六态完全一致；`FR-11` 的「审查中」由「待审行且 `attempt_count > 0`」兑现，不需要改动需求。
- 为什么这么做：单事务到底会让数据库事务跨越一次网络调用（SMTP 超时可达数十秒），期间同批的行全被锁住；且未提交的中间状态在外部不可观测，「审查中」无法兑现。
- 代价如实登记：调用已完成、结果尚未提交时崩溃，若 `attempt_count < 4`，该行会被重新认领并重跑——审查无害，发信即重复投递一次（SMTP 可能其实已成功），与「发送中宕机再投一次」是同一个窗口；若恰是第 4 次尝试，则由收割支推进终态（不重跑、不产生第 5 次尝试），代价是这一封的最终结果可能被记为失败而 SMTP 其实已经成功。

### 3.1 提交与待审行的产生
- `POST /v1/messages` 校验通过后写一行待审行（`direction = outbound`），`id` 与主键同值。
- `Message-ID` 在提交时由 `core/` 的纯逻辑生成（无数据库往返、可单测），形如 `<{UUID}@{发件域}>`：UUID 部分用该行的主键 `id`；域名部分取 `MAIL_FROM_ADDRESS` 里 `@` 之后的内容，若该配置带显示名则先取最后一个 `<...>` 内的地址再取域（不新增配置项）。
- 写入待审行后原样搬运到目标行，使 `{id} → Message-ID` 的映射从第一刻起稳定——出站邮件的 `Message-ID` 是入站回复关联的唯一依据。

### 3.2 流① 审查出站
- 认领：`pending_messages` 中 `direction = outbound`、`attempt_count <= 4` 且 `next_attempt_at <= now()` 的行；`attempt_count < 4` 时按 §3 阶段① 递增并推退避，`attempt_count = 4` 时走收割支（不调用审查，直接置终态）。
- 调用：事务外调用审查接口（契约见 §4.2，`FR-3`）。
- 落库：同一事务内依次「写入目标行 → 删除待审行」，结果事务**不写** `attempt_count` 与 `next_attempt_at`（目标行是 `messages` 时，**建行**即把这两列写成 `0` 与 `now()`，§2.4）。过审 → `messages` 一行 `sending`；判定拒或出错用尽重试 → `rejected_messages` 一行（`source` 记 `review_rejected` / `review_error`）；收割支 → 同样写 `rejected_messages`，`source = review_incomplete`。目标行记 `reviewed_at`，`created_at` 原样搬运、保持为原始提交时刻，跨表 ID 同值。撞唯一键 / 主键时视为「这一封已经处理过」：只删待审行，不重复写目标行。
- 为什么这么做：待审表因此只承载在审队列，不随邮件量增长，也不需要单独的清理策略（清理只需覆盖邮件表与拒审表）。
- 审查「判定不通过」与「环节出错（异常 / 超时）」两条路径都移入 `rejected_messages`，由该表的 `source` 列区分来源，而调用方从 API 看到的对外终局都是「拒审」，于是待审表仍是纯队列：不需要 `status` 列，也不需要第二套清理逻辑。
- 审查出错用尽重试后不置 `failed`：`failed` 在 `FR-5` / `US-4` 里指投递失败，用它表达审查器故障会把系统故障误报成邮件系统故障，而这封邮件根本没到 SMTP。

### 3.3 流② 发信
- 认领：`messages` 中 `direction = outbound`、`status = sending`、`attempt_count <= 4` 且 `next_attempt_at <= now()` 的行（`FR-4`）；`attempt_count = 4` 时走收割支（不发起 SMTP，直接置终态 `failed`）。
- 调用：SMTP 直连（企业邮箱 / 自建 MTA），用 Python 标准库 `smtplib` 并通过 `asyncio.to_thread` 调用（与 IMAP 侧同构），不新增运行时依赖。主机、端口、账号、口令、是否 STARTTLS 等参数全部来自 `.env`（见 §4.5）。
- 落库：结果事务只写 `status` / `last_error_class` / `last_smtp_code` / `sent_at`，**不写** `attempt_count` 与 `next_attempt_at`；状态写入一律**带谓词**（只允许 §2.3 声明的起点），`rowcount = 0` 按幂等忽略。
- 成功 → `status = sent` 并写 `sent_at`；失败 → 写 `last_error_class` 与 `last_smtp_code`：`attempt_count` 已达 4 则置终态 `failed`，未达上限则 `status` 保持 `sending`（退避由认领时写好的 `next_attempt_at` 承担）。

| 情形 | 处置 |
|---|---|
| `5xx` | 计为 `permanent`，直接置终态 `failed`，不再消耗剩余重试次数（`AC-34`） |
| `4xx` 与连接 / 超时类故障 | 计为 `transient`；未达 4 次尝试时保持 `sending` 等下一轮认领，已达 4 次则置终态 `failed` |
| 进入终态后 | 不做任何通知；`NFR-3` 与 `spec.md` §7 已定不做回调推送，调用方靠轮询 `GET /v1/messages/{id}` 得知终态 |
| 退信到达 | **只作用于已发出的行**（`sent_at` 非空）：置 `failed`；`last_error_class` 按 DSN 的 `Status` 映射（`5.x.x` → `permanent`、`4.x.x` → `transient`、**取不出时（`Status` 缺失或非 4/5 开头，含 `2.x.x`）复用 `internal`**——所有者裁决：该支是对端退信、只是读不出子类，不另设第四类、也不写 `NULL`；已否决方向：新增 `unknown` 分类、写 `NULL` 留空）；`last_smtp_code` 留空（本次没有 SMTP 交互）、`attempt_count` 不变、`sent_at` 保留不清空。**`sent_at` 为空的行（`sending`）收到退信不置状态**：按受控原因 `dsn_target_not_sent` 计数丢弃、不打断重试——退信的前提是某台 MTA 已接受该邮件，`sending` 行收到退信只可能来自「SMTP 已接受、结果事务未提交」的窗口，此时继续重试比钉死更正确 |

- 失败分类的判据就是 SMTP 标准本身给 4xx / 5xx 的分工，不需要维护「哪些码算确定性拒绝」的猜测量表（`FR-5` 已声明一期只区分「可重试 / 不可重试」两类）。
- 退信到达后，「曾经被接受、事后被退信」＝ `sent_at` 非空 + `status = failed`，「从未被接受」＝ `sent_at` 为空；这段历史是事实的推论，不需要专门的历史列（`bounced_at` 因此不引入，见 §6「分拣与丢弃」），也不新增 API 字段（`AC-17` 本来就要求响应携带时间字段）。这条推论成立的前提正是「退信只作用于 `sent_at` 非空的行」。代价登记：退信到达时刻不入库，无法统计退信延迟。

### 3.4 流③ 拉取入站
- 认领：没有可认领的行——增量进度由 `imap_cursors` 里的 `UIDVALIDITY` 与已处理的最大 UID 决定。
- 调用：IMAP 拉取（单个专用回复邮箱），用 `References` / `In-Reply-To` 关联原邮件（`FR-7`）。
- 落库：拉回的邮件必须先分拣再落库，四类去向见下表（`FR-9`）。

| 分拣结果 | 去向 |
|---|---|
| 关联回某封出站邮件的用户回复 | 写入 `pending_messages`（`direction = inbound`，带 `parent_id` 与 `mailbox` / `uidvalidity` / `uid`），再经流④ 审查 |
| 关联回某封出站邮件的退信 | 按 §3.3 的「退信到达」一行写该出站行，退信本身不入库；DSN 指向的出站行已被清理 → 归入丢弃（`dsn_target_gone`） |
| 阅读回执（MDN） | 剔除、丢弃，绝不允许被当成用户回复 |
| 其余（关联不上、结构无法解析） | 一律丢弃、不入库，只按原因分类计数 |

入站行的内容字段取值：

| 入站行列 | 取值来源 |
|---|---|
| `recipient` | 回复的 `From`（回复者地址，外部可控、原样入库） |
| `subject` | 回复的 `Subject`（缺失时存空串） |
| `body` / `body_format` | 优先取 `text/plain` 部件；没有则取 `text/html` 并置 `html`；两种部件都没有 → 丢弃（`unparsable`） |
| `created_at` | **该回复的落库时刻**（本行产生时刻，§2.5），不是父行的提交时刻 |
| `attempt_count` / `next_attempt_at` | 建行时写初值 `0` 与 `now()`（§2.4） |
| `recipient` 的长度 | `From` 超长时**截断后再入库**：外部可控的头值不得把整封写不进去 |
| 缺 `From` 的回复 | 丢弃并计入 `no_reply_from`（`recipient` 是 NOT NULL，没有可入库的收件人） |

- 游标推进规则：**逐封独立事务**——该封的分拣落库（写待审行 / 置失败 / 计数丢弃）与「把 `last_uid` 推到该封的 UID」在**同一个事务**里完成（处理完再推，不是拉之前）；单封异常只吞掉该封并计入受控原因，绝不中止整批；撞入站唯一键即跳过该封并计入 `duplicate`，**不得让冲突把游标推进一起回滚**（否则每一轮都会重撞同一封、入站永久停摆）。游标行不存在时初始化为该信箱当时的 `UIDNEXT - 1`；`UIDVALIDITY` 变化时**也重置为当时的 `UIDNEXT - 1`**（不是 0 / 1）。
- 这条规则的两个后果：① 轮换后不会把轮换前的邮件以新 `uidvalidity` 重拉，因此**不会出现同一封回复入库两条**——原来那条「唯一约束兜底」的说法在轮换场景下并不成立，缺口由这条规则补平；② 代价如实登记：初始化或轮换那一瞬间已在信箱里的邮件不会被回收。
- **每个 tick 处理封数有上限**：区间取 `last_uid + 1` 到 `min(UIDNEXT - 1, last_uid + BATCH_SIZE)`，剩余留到下一个 tick（`NFR-2` 的「每批条数是配置项」对流③ 同样成立）。没有上限时，停机一天后重启的第一次 tick 要在一个 tick 里解析整个积压，会挤占其余四条流的连接池与事件循环。
- 游标管效率、唯一键管正确性：唯一键仍是 `mailbox` + `UIDVALIDITY` + `UID`（§2.2），只是它只能兜住同一 `UIDVALIDITY` 内的重复；`Message-ID` 不得用作幂等键或去重依据。
- 分拣的三层落点：`providers/imap/` 只把邮件取回并解析成中性结构（头字段、MIME 部件、UID 三要素），不做任何判定；`core/` 放纯判定函数（DSN 结构判定、MDN 判定、`References` / `In-Reply-To` 的解析与关联匹配）。
- `flows/` 的入站步骤编排「取回 → 判定 → 落库 / 置失败 / 计数丢弃」，并靠 `mailbox + uidvalidity + uid` 的唯一约束兜住同一 `UIDVALIDITY` 内的重复；分拣规则因此可脱离真实 IMAP 单测，换 IMAP 客户端也不影响分拣。
- 退信识别按结构判断，不靠主题或正文猜：DSN 的判定依据至少包含 `multipart/report` 容器与 `message/delivery-status` 部分；`Status: 5.x.x`（永久）与 `4.x.x`（临时）都算失败，置对应出站邮件为发送失败（写列口径见 §3.3 的「退信到达」一行，`FR-6`）。
- 原邮件靠 DSN 内嵌的 `message/rfc822` 部分取出；取不出原邮件标识的退信被丢弃，调用方因此看不到这次失败——登记为已知缺失。退信检测是尽力而为：部分 SMTP 网关会丢弃或延迟 DSN，临时失败也可能不产生退信，因此退信不得作为「是否送达」的唯一判据。
- 回路只承认「点回复」：用户若不点「回复」而新写一封邮件，它关联不上任何出站邮件，因此不会入库、agent 也查不到。这是明确接受的代价，不是缺陷；要支持它需要另做关联设计。
- 丢弃日志的口径：丢弃时按原因分类计数，仍然只记条数、不落任何可回溯字段（发件人 / 收件人 / 主题 / 正文 / UID / 原始头值一律不记）。
- 十一类标识符：`no_reference`（没有 `In-Reply-To` / `References`）、`no_match`（引用存在但匹配不到本服务的出站邮件）、`mdn`（阅读回执剔除）、`dsn_without_id`（退信取不出原邮件标识）、`dsn_target_gone`（退信指向的邮件已不存在）、`dsn_target_not_sent`（退信指向的行尚未发出：不置状态、不打断重试）、`duplicate`（撞入站唯一键，这一封已经处理过）、`no_reply_from`（回复没有 `From`，没有可入库的收件人）、`malformed_reference`（`In-Reply-To` / `References` 畸形到无法解析）、`reply_target_gone`（写回复行时 `parent_id` 外键违例：父行已被清理）、`unparsable`（结构无法解析）。
- 有丢弃时记**一条**事件 `inbound_discarded`，字段为十一类计数 + 总数（契合 `AC-13` 与 `AC-28` 的断言）；连接失败或单封处理异常时额外记一条受控错误事件（受控枚举 + 计数，不含任何可回溯字段）。
- 盲区因此从「不知道发生了什么」缩到「知道是哪一类丢弃、但不知道是哪一封」，且不越过 `NFR-5` / `NFR-10` 的红线。**已登记的残留盲区**：正常 tick（连接成功、拉回 0 封、无丢弃）不留任何记录，因此「用户没点回复」与「游标停摆但连接正常」仍不可区分；只记异常事件是有意为之（日志体积优先），要补的话是「每个 tick 一条纯计数事件」。

### 3.5 流④ 审查回复
- 认领：`pending_messages` 中 `direction = inbound`、`attempt_count <= 4` 且 `next_attempt_at <= now()` 的行，短事务与事务边界同 §3.2（含结果口径与收割支）。
- 调用：事务外调用同一个审查接口，做同样的安全审查（`FR-8`）。
- 落库：同一事务内过审 → `messages` 一行 `received`（建行时 `created_at` 取落库时刻、`attempt_count = 0`、`next_attempt_at = now()`）；判定拒或出错用尽重试 → `rejected_messages` 一行，`source` 记来源，并按 §2.1 **带上该回复的主题与正文**供复核；收割支 → 同样写 `rejected_messages`，`source = review_incomplete`；随后删除待审行。结果事务同样不写 `attempt_count` 与 `next_attempt_at`。写回复行时若 `parent_id` 外键违例（父行已被保留期清理），按 `reply_target_gone` 计数丢弃，**绝不写成拒审**。

### 3.6 保留期清理
- 保留期清理是第 5 条后台任务，范围只覆盖 `messages` 与 `rejected_messages`——`pending_messages` 是纯队列，无需清理；清理间隔由配置项 `POLL_CLEANUP_SECONDS` 定（默认 3600 秒）。判据是各行的 `created_at`（本行产生时刻，§2.5）。
- 删 `messages` 的出站行时必须**排除仍有入站待审行指向它的行**（`NOT EXISTS`）：`pending_messages` 不参与清理，父行先被删会留下一条指向已删行的待审行，流④ 之后写回复行即撞 `parent_id` 外键、重试到上限后把用户的**正常回复误报成拒审**。代价登记：极老的邮件可能因一条卡住的待审行而延后清理（配合 §3 的收割支，卡住的行本身会被推进终态，这个代价基本不兑现）。
- 删除分批进行：每批按 `BATCH_SIZE` 取行并 `SELECT ... FOR UPDATE SKIP LOCKED` 后删除；与流② 正在投递的行并发时，被跳过的行留到下一轮（结果事务写 0 行按幂等忽略、不报错）。
- 删除邮件行时由 `parent_id` 外键的级联把回复一并删掉，因此 `AC-22` 的「执行一次清理动作」就是「`await` 一次 tick」，不依赖任何外部定时配置。

### 3.7 崩溃与重启后的恢复
恢复只依赖状态字段与 `next_attempt_at`：`messages` 的 `status` 与 `next_attempt_at` 决定流② 下一批处理哪些行，`pending_messages` 的 `next_attempt_at` 决定流① / 流④ 重跑哪些行，`imap_cursors` 决定流③ 从哪个 UID 继续，不需要额外的超时回收机制（`FR-13`、`NFR-9`）。**被中断在第 4 次尝试上的行同样不需要额外机制**：认领谓词含 `attempt_count = 4`，重启后由收割支推进到终态（§3 阶段①）；否则它既不会被重跑、也不会进终态，会永久停在「已提交」/「审查中」。

调度载体与并发抢占硬约束属跨特性底座，见 `docs/research/02-runtime-and-scheduling.md`。

## 4. 集成契约
### 4.1 对外 API

**端点的路径、请求与响应字段、类型、必填、取值枚举与取值约束、错误响应，唯一真源是 `docs/api-contract.yaml`（OpenAPI 3.1）**——本节不重复任何字段定义，只写设计口径与取舍理由。四个端点的用途：`POST /v1/messages` 提交（`AC-1`）、`GET /v1/messages/{id}` 查状态、`GET /v1/messages/{id}/replies` 查回复（可为空集合）、`GET /healthz` 健康检查（`FR-14`）。

- **`/healthz` 的语义是「进程可用」**（HTTP 可达 + 一次轻量数据库探测），**不表达五条后台流是否在跑**——`AC-20` 的口径不变，流是否推进靠日志与独立探针。
- **HTTP 状态码只表达请求本身是否成功，不承载业务语义**：「该 ID 不存在」与「该 ID 存在但尚无回复」都返回 200，靠响应体的 `found` 区分。不用 404 的理由是这两种情形对调用方都不是错误——前者可能是提交后尚未提交事务，后者是**正常状态**（用户还没回）；用状态码区分会迫使调用方把「还没回」当异常处理。
- **回复查询用 keyset 游标，不用 `limit` / `offset`**：新回复到达会让偏移窗口整体漂移，导致翻页时漏读或重读，与 `US-3` / `AC-16`「重复执行且结果稳定」直接冲突；keyset 的排序键固定为 `received_at ASC, id ASC`，游标对客户端不透明。
- **时间字段固定为 UTC RFC 3339 六位小数 + `Z`**（`NFR-7`、`AC-17`）：库中是 `timestamptz`，序列化时先转 UTC 再补零到六位，调用方不必处理本地时区或可变的小数位数。
- **状态取值只有一套六态，库内与对外同名同义**（标识符、中文名与各状态的物理落点见 §2.1 落点表与 §2.3）；状态机沿「已提交 → 审查中 → 投递中 → 已发出 / 发送失败」**单调推进**（拒审是从「审查中」分出的终局），调用方不必处理状态倒退。
- **六态判定在单个事务快照内完成**（三次查询同一快照）：否则会读到「跨表搬运中」的中间组合（待审行已删、目标行未可见），把一封正常邮件误报成 `found: false`。
- **查询端点的 `{id}` 空间只含出站邮件**：`MessageId` 是「出站邮件的对外 ID」，六态枚举也不表达入站回复行的终态 `received`（§2.3），因此拿入站行 id（对外表现为 `Reply.id`）查两个查询端点一律 `found: false`、不抛异常、**不另设错误码**（对外措辞见 `docs/api-contract.yaml` 的 `MessageId` 与 `GET /v1/messages/{id}` 的说明）。**已否决方向**：① 为回复 id 扩展 `MessageState` 枚举——把入站行塞进出站状态机，「库内与对外同一套取值」当即不成立；② 按行所在表给不同答案（入站待审行答一个值、过审搬进 `messages` 后答另一个值）——同一 ID 的答案随审查推进而漂移，与 `FR-10`「重复查询返回相同结果」直接冲突。
- **一期不做 API 鉴权**：位置在 `api/v1/dependencies.py` 预留，接真实校验时不重排路由结构（`open-questions.md` F3）。

- 审查失败的退避等待期间（`attempt_count > 0`、`next_attempt_at` 在未来）对外也报「审查中」。
- HTTP 状态码只表达**请求本身是否成功**，不承载业务含义。一律 422 且不落库：缺少收件人（`AC-2`）、携带两个收件人（`AC-3`）、`recipient` 给成列表或逗号分隔的多地址、地址非法（无 `@` 或含空白）、`recipient` 或 `subject` 含 CR/LF（头注入，`docs/security.md` 已列为安全问题）。
- 同样 422 且不落库：`body_format` 非法、主题或正文超限（`AC-33`）、路径参数 `{id}` 不是合法 UUID（请求本身不合法，与「状态码只表达请求是否成功」一致）。
- 未知字段一律**忽略、不报错**：`AC-4` 的「显式携带发件人字段仍能发出、`From` 不变」由此自动成立，不需要专门写忽略逻辑。
- 查询端点对「该 ID 不存在」与「该邮件存在但尚无回复」**都返回成功状态**：「不存在」是 `found: false` + 空 `replies`，「存在但尚无回复」是 `found: true` + 空 `replies`（`AC-31`、`AC-35`、`FR-10`、`NFR-8`）。**不得用 `404` 之类的状态码区分业务语义**。
- 时间格式：对外返回的时间字段固定为 UTC 的 RFC 3339 字符串、固定六位小数并以 `Z` 结尾；库中对应的列类型为 `timestamptz`。契约与存储因此一一对应，调用方不需要处理本地时区或可变的小数位数（`NFR-7`）。
- 回复分页用 **keyset 游标**：`?limit=N&after=<received_at>,<id>`（缺省从第一页开始），响应带 `next_cursor`（无更多时为 `null`）。排序固定为 `received_at ASC, id ASC`，游标正好落在回复查询索引 `(parent_id, received_at)` 上；每页条数上限由配置项 `REPLIES_PAGE_SIZE` 定（默认 100）。**不用 `limit` / `offset`**：新回复插入会让偏移窗口漂移，与 `FR-10`「重复查询返回相同全集与相同顺序」冲突。一封出站邮件的回复数无上限，因此分页是必须的，不是可选项。

### 4.2 审查接口
输入是一封待审邮件的完整内容，字段固定为下表（一期空实现忽略全部字段）——审查要审的正是它即将决定放行的那份内容，所以内容必须完整传入；ID 是 `NFR-5` 要求的「用 ID 引用邮件」所需要的，适配器记录自己的判定或告警时要靠它，而 ID 不携带任何内容信息。

| 字段 | 说明 |
|---|---|
| `direction` | `outbound` / `inbound`；真实审查器据此对出站与入站采用不同策略 |
| `message_id` | 该邮件的对外 ID（即主键），不携带任何内容信息 |
| `recipient` / `subject` / `body` / `body_format` | 即将被放行的那份内容 |

单次调用受 `REVIEW_TIMEOUT_SECONDS` 约束（默认 10 秒，见 §7）；超时按「抛异常」一行处理。

- 接口以中性对象传递，既不接收 ORM 行，也不接收数据库会话——`providers/` 不得依赖 `db/`，否则适配器就与存储实现绑死了。
- 实现由 `providers/review/` 的工厂函数按配置选取（一期恒为空实现）；测试通过依赖覆盖 / monkeypatch 替换成「总是拒绝」的假实现（`AC-6` / `AC-12` 依赖它）。**一期不存在可以关掉某条流（尤其审查流）的运行时配置。**
- 输出是受控枚举判定（`pass` / `reject`），一期空实现恒返回 `pass`。不设原因文本、不设风险分类字段：原因文本若要入库，与 §2.4「错误里不落任何自由文本」的取向冲突；分类字段目前没有任何消费方。将来真实审查需要表达力时，属新的决策（按 `AGENTS.md` §6 更新本文件）。

返回 `reject` 与抛异常是两条不同的路径，由接口本身区分，因此接口不需要任何「错误」字段：

| 路径 | 含义 | 处置 |
|---|---|---|
| 返回 `reject` | 判定为拒 | 不投递（出站）/ 不入库（入站），直接写 `rejected_messages` 表，`source` 记 `review_rejected` |
| 抛异常 | 审查环节出错 | 按固定重试语义重试（同一套重试算术，见 §3 的阶段① 与结果口径），用尽后同样写 `rejected_messages` 表，`source` 记 `review_error` |

两条路径在调用方看来都是「拒审」一项，与 `FR-11` / `AC-6` / `AC-12` 一致。审查结果如何影响状态（写目标行、删待审行）由 `flows/` 的审查流编排，不放适配器内——适配器只回答「过 / 不过」。

### 4.3 发件人身份与回复路由
发信的 `From` **固定取自服务配置** `MAIL_FROM_ADDRESS`，**绝不允许调用方指定**；服务端必须显式写入 `Reply-To`，其值等于 IMAP 拉取所用的信箱，即配置项 `REPLY_TO_ADDRESS`，同样不允许调用方指定（`FR-2`）。

一期内容形态只有纯文本 / HTML，没有抄送密送、没有自定义邮件头、不支持附件，因此「回复能否回到可被 IMAP 拉取的信箱」只取决于 `Reply-To` 或 `From` 指向哪里。文档与代码中一律使用占位符表示地址（如 `<from-address>`、`<robot-mailbox>`）：仓库内不出现任何真实邮箱地址、主机名或账号。

- 为什么 `From` 必须固定：若调用方可以指定发件人，本服务就成了可伪造任意发件人的工具，服务方无法为发出的邮件背书；`From` 一旦与 IMAP 拉取的信箱不一致，回复回路会静默断掉，固定成配置项等于把这个变量从系统里移除。
- 显式写 `Reply-To` 的目的不是「让回路能通」：按 RFC 5322，收件人点「回复」时客户端把回复发往 `Reply-To`（若存在），否则才发往 `From`；只要 `From` 本身就是被拉取的那个信箱，回路本来就是通的。
- 它的真正目的是不让回路隐式依赖「`From` 恰好等于被拉取的信箱」——不显式声明时，此后任何一次改动 `From`（例如换成不接收回复的发件身份或某个部门别名）都会让回复静默断掉：没有报错、没有告警，只是回复再也不会回来。显式声明把「回复去哪」变成一行可被检查、可被校验的邮件头，并让对外发件身份可以独立演进。
- 代价：多一个必填配置项（`REPLY_TO_ADDRESS`），漏配就少一层保障。这条决策的价值靠纪律维持——因为不写 `Reply-To` 也能通，若有人以「简化配置」为由删掉它，功能测试仍然会通过，只有在 `From` 发生变更时才会暴露问题；因此一致性校验已做成硬约束：启动即校验、不一致就拒绝启动（见 §4.4）。

### 4.4 启动即校验
启动时必须校验三条，任一不满足就**拒绝启动**（fail-fast：非零退出、不监听端口），不做「告警后继续跑」（`AC-32`）：

1. 「被拉取信箱地址」（`IMAP_ADDRESS`）与 `REPLY_TO_ADDRESS` 相等；
2. `MAIL_FROM_ADDRESS` 能被解析出邮件域——`Message-ID` 的域取自它（§3.1），`<uuid@>` 这种残缺值必须在启动时就被拦住；
3. `REPLY_TO_ADDRESS` 本身是合法地址（含 `@`、无空白）。

校验的**判据**在 `config.py`（`validate_startup`），**调用点**在 `main.py` 的 lifespan 显式启动步骤，不放在 import 期：多 worker 或 `--reload` 下，import 期异常会表现为「每个 worker 各拒一次」的无尽重启循环，而不是一条清晰的启动错误。

- 「被拉取信箱地址」——拿去与 `REPLY_TO_ADDRESS` 比对的那个值，不是 IMAP 的信箱文件夹——由一个显式配置项 `IMAP_ADDRESS` 声明，不默认取 `IMAP_USERNAME`：IMAP 登录名不保证是邮箱地址，而「靠两个字段恰好相等」正是本设计已经否决过的隐式依赖。
- 为什么这么做：`Reply-To` 与所拉取信箱不一致时，回复回路是静默断掉的（邮件照发、回复永远拉不回来）；把它变成一次启动失败，是唯一不依赖「人记得检查」的保证方式。
- 代价如实登记：部署时多一个必填配置项（连同上面第 2、3 条校验，共三条判据）；配置写错时服务直接起不来（这是有意为之）。校验信息里只出现**配置项名**，不出现地址本身（`NFR-5`）。

### 4.5 SMTP / IMAP 参数来源
SMTP 与 IMAP 的主机、端口、账号、口令、是否 STARTTLS、被拉取信箱地址与信箱文件夹等参数全部来自 `.env`，只经 `config.py`（pydantic-settings）这唯一入口读取，仓库只提交 `.env.example` 占位（见 `AGENTS.md` §7 与 `NFR-4`）；各字段的落点与命名见 §7 落地位置。

### 4.6 日志输出格式
`LOG_FORMAT` 的两条渲染分支都必须遵守同一个口径：**异常由 processor 渲染进结构化字段 `exception` 一次，渲染器只负责排版**。落点是 `logging.py`，下面两条配置必须同时成立，删改其中任一条即破坏本节承诺：

1. `ProcessorFormatter(keep_exc_info=False)`。
2. **console 分支的 `final_processors` 必须在 `ConsoleRenderer` 之前包含 `format_exc_info`**——漏掉它，异常就改由 `ConsoleRenderer` 自行渲染，两种格式不再同源，「渲染器只负责排版」当即不成立。这条配置的可观察判据有两条，回归断言（`tests/unit/test_logging_output.py`）用它们各自钉一件事：
   - **判据①：行首形态。** 去 ANSI 后必须**恰有一行**的行首是 `Traceback (most recent call last):`——这是 plain traceback 的形态；异常改由 `ConsoleRenderer` 渲染时，它被画进带 `│ ` 前缀与面板标题的 rich 框里，没有任何一行的行首是它。这条判据**不依赖任何字形**。
   - **判据②：不得出现面板框线**（`╭` / `╰`）。它直接对应下面的「不带重复框」。
   - 两条是**独立通道**：① 管「异常是不是 processor 渲染出来的 plain traceback」，② 管「有没有退化成面板」。② 的取值依赖 rich 的字形（换版本就可能失真），① 不依赖字形，是更稳的那条——改动 rich 相关依赖时据这两条分别判断。

两种格式都不允许把同一个 traceback 输出两份。

两种格式各自的对外承诺：

- **`json` 保证 JSON-lines**：每条日志记录恰好一行合法 JSON；异常信息只出现在该行的 `exception` 字段里（含完整 traceback），字段内部换行按 JSON 字符串转义。**流里不得出现裸文本行**——按行 `json.loads` 采集的采集器一遇到非 JSON 行就报错，所以「每条记录一行合法 JSON」是契约，不是实现细节。
- **`console` 是人类可读文本**：事件一行，异常一份（紧随其后的 plain traceback），不带重复框，也不带第二份纯文本。

**实测证据**（同一探针：`INFO` 级别，标准库 `logging.getLogger("third.party").error(<事件>, exc_info=True)` 记一次 `ValueError`，采集 `sys.stderr`；「行」指非空行）：

| 配置 | `json` | `console` |
|---|---|---|
| 两条都满足（当前实现） | 1 行 = 恰好 1 行合法 JSON，`exception` 字段含完整 traceback，逐行 `json.loads` 全部通过 | 5 行 = 1 行事件 + 1 份 traceback（`raise ValueError(...)` 出现 1 次、行首命中 1 次、无面板框线） |
| **只违反第 1 条**（`keep_exc_info=True`，console 链正常） | 5 行 = 1 行合法 JSON + 4 行裸文本；逐行 `json.loads` 在 4 行上报错，输出流不是 JSON-lines | 9 行 = 事件 1 行 + **2 份** plain traceback（多出的是标准库 `logging.Formatter` 追加的那份；行首命中 2 次） |
| **只违反第 2 条**（console 链缺 `format_exc_info`，`keep_exc_info=False`） | 1 行 = 合法 JSON，**不受影响** | 32 行：异常改由 `ConsoleRenderer` 渲染成 rich 面板（行首命中 **0** 次、面板框线 4 个） |
| **两条都不满足** | 5 行 = 1 行合法 JSON + 4 行裸文本 | 36 行 = rich 面板一份 + 标准库追加的 plain traceback 一份（行首命中 1 次、面板框线 4 个） |

**两条成因必须分开记，不要合并挂到某一条配置名下**：`json` 侧的 4 行裸文本只由第 1 条被违反造成（第 2 条只影响 `console`）；`console` 侧的「第二份 traceback」与「rich 面板」是两件不同的事——前者来自 `keep_exc_info=True`（标准库追加 plain 文本），后者来自第 2 条被违反（`ConsoleRenderer` 自己渲染）。因此「修复前」只有在**两条都不满足**时才是一个确定的状态。绝对行数随探针的**栈深度**变化，只有同一探针内的数字可比；`console` 的 5 行在两种探针下一致。

**已否决方向**：

- **保留 `keep_exc_info=True`** —— 它把 `exc_info` 留在 `logging.LogRecord` 上，标准库 `logging.Formatter` 于是再追加一份纯文本 traceback：`json` 流被掺进 4 行非 JSON 文本，直接破坏上文的 JSON-lines 契约（按行采集立即报错）；`console` 同一 traceback 出现两份（同一探针下 5 行涨到 9 行；另一份由标准库 `logging.Formatter` 追加，plain 文本）。它并不产生「保住 traceback」的收益——异常已在 `format_exc_info` 那步渲染进 `exception` 字段，这个 `True` 只制造第二份；它本是对 structlog `keep_exc_info` 默认值（默认即 `False`）的显式覆盖，由来是照抄官方「用 structlog 渲染标准库日志」配方时留下的安全默认，本意「别把 traceback 丢了」已由「异常落在结构化字段里」覆盖。
- **两条分支各自渲染异常**（`json` 走 `format_exc_info`、`console` 只靠 `ConsoleRenderer`）—— 同样能做到「不重复」，但两模式口径不同：一边把异常渲染进字段，另一边由渲染器自行渲染，§4.6 的「任何分支都只渲染一次」就得靠两套写法各自维持。统一到 processor 链上，口径写在配置里而不是写在纪律里。代价如实登记：`console` 的 traceback 由 rich 框（带颜色、带边框、含 locals）退化为 plain traceback，换来的是与 `json` 同源、异常渲染口径只在 processor 链这一处定义。

## 5. 依赖选型
| 库 | 许可 | 用途 | 换来的好处 | 代价 |
|---|---|---|---|---|
| `IMAPClient` | New BSD | IMAP 客户端；同步库，入站拉取流通过 `asyncio.to_thread` 调用，不把阻塞调用放进事件循环 | 把最容易出错的那一段外包出去：IMAP 响应解析、UID 语义、多行与字面量、时区处理都由库负责，我们只做 `email` 解析与分拣——而分拣正确性正是 `AC-11` / `AC-15` 的考点 | 不带 `py.typed`（其自身的 mypy 配置就把 `imapclient.imapclient` 标为 `ignore_errors`），因此按 `docs/research/03-dependencies-and-toolchain.md` 的既定程序在 `[[tool.mypy.overrides]]` 中按 module 精确放行并留下理由，不整体关闭 mypy |
| `asyncpg` | Apache-2.0 | 数据库驱动，配合 SQLAlchemy 异步方言（连接串形如 `postgresql+asyncpg://…`，与 `config.py` / `.env.example` 现有占位一致） | 异步访问 PostgreSQL 的标准组合 | 不带 `py.typed`——代码直接引用 `asyncpg.exceptions` 区分唯一约束冲突时，按 `docs/research/03-dependencies-and-toolchain.md` 的既定程序在 `[[tool.mypy.overrides]]` 中按 module 精确放行并留下理由，不整体关闭 mypy；Alembic 迁移走异步外壳（用 SQLAlchemy 的异步引擎跑迁移） |
| `SQLAlchemy` | MIT | `db/` 的 ORM 模型、引擎与会话 | 本次新增的运行时依赖，许可无 copyleft 义务 | — |
| `Alembic` | MIT | `migrations/` 迁移 | 同上 | — |

发信侧用 Python 标准库 `smtplib`，与 IMAP 侧同构通过 `asyncio.to_thread` 调用，不引入新的运行时依赖，因此不占上表的行。

选型顺序是许可优先于类型便利：`psycopg3` 自带 `py.typed` 且同一库覆盖同步与异步（Alembic 迁移因此更省事），但许可是 LGPL-3.0-only，交付镜像时要承担 copyleft 相关义务，与本项目「私有 + MIT、避免许可摩擦」的取向冲突（许可取向见 `docs/research/06-naming-licensing-and-governance.md`）。

这是两者之间唯一无法用本地纪律化解的代价，`py.typed` 带来的便利不值这一项。否决记录见 §7「依赖选型」。

### 实测证据
事实来源：各库的 PyPI 页面与仓库内的 `pyproject.toml` / `LICENSE` / `py.typed`。查得当前版本为 `IMAPClient` 4.1.0、`aioimaplib` 2.0.1、`asyncpg` 0.32.0、`psycopg` 3.3.6。

| 库与版本 | 许可（实测） | `py.typed` | 其他实测结果 |
|---|---|---|---|
| `IMAPClient` 4.1.0 | PyPI 元数据标 New BSD | 不存在 `src/imapclient/py.typed`（HTTP 404） | 其 `[tool.mypy.overrides]` 把 `imapclient.imapclient` 与 `imapclient.livetest` 标为 `ignore_errors = true` |
| `aioimaplib` 2.0.1 | `pyproject.toml` 写 `license = "GPL-3.0"` | — | — |
| `asyncpg` 0.32.0 | `LICENSE` 为 Apache-2.0 | 不存在 `asyncpg/py.typed`（HTTP 404） | 该版本于 2026-10 发布，提供 3.13 / 3.14 轮子（仍在发版） |
| `psycopg` 3.3.6 | PyPI 元数据标 LGPL-3.0-only | `psycopg/psycopg/py.typed` 存在（HTTP 200） | — |

## 6. 已否决方向
本节是附录性质，按主题分组，每条只写「否决了什么 —— 关键理由」。

### 主键与对外 ID
- 数据库自增 `bigint` 作主键并直接对外暴露 —— 一期不做 API 鉴权，ID 可枚举即等于内网任意主机可顺序遍历读取他人的邮件状态与回复内容（`GET /v1/messages/{id}/replies` 会返回回复内容）。
- 时间有序 ID（UUIDv7 / ULID）作主键 —— Python 3.13 标准库不提供，为此新增运行时依赖不划算；本项目「每 10 秒一批、总量小」的量级下，索引局部性收益不可见。被否决的是「为此引入依赖」，不是时间有序 ID 本身，量级上升后可重新评估。
- `bigint` 主键 + 对外不可枚举 token 的两套 ID —— 仓储层与接口契约都要双份维护，用复杂度换本项目不需要的索引收益。

### 表与字段形态
- 回复只存 `in_reply_to` 文本列、按文本值 join 原邮件 —— 没有外键完整性，`NFR-8` 的「邮件与回复一起移除」退回应用层两步删除，可能漏删回复或留下查不到的孤儿行；换来的只是一次唯一索引查询。
- 用单个 `last_error jsonb` 或单个受控短字符串承载最后一次错误 —— 约束只能靠约定与 `CHECK` 维持，任何一次「顺手把 `str(exc)` 存进去」都不会被类型拦住，防线只剩事后测试；而错误记录恰恰是 `NFR-5` 红线最需要结构性保障的地方。
- 用邮件 `Date` 头作回复排序键 —— 该头由发件人客户端生成，可被改写、可为空、时区可错，`FR-10` / `AC-15` 的顺序稳定性会随客户端行为漂移。
- 用 IMAP `INTERNALDATE` 作回复排序键 —— 虽比 `Date` 头可信，但多一条外部依赖（服务器时区、客户端须暴露该字段），而本项目没有任何消费方需要绝对到达时间；顺序稳定只需要「我们自己写下的时刻」。
- 过审后删行且不记任何审查时刻 —— 省一个可空列，代价是「卡在审查」与「审查耗时」彻底不可排查，`FR-15` 的 3 分钟时效失去第一个观察点。
- 用布尔 `is_html` 表达正文格式 —— 只能表达两值，加第三种格式要改列类型并全量改代码，而枚举只需加一个值。
- 正文拆成 `body_text` / `body_html` 两列并加「恰好一个非空」约束 —— 为 `spec.md` §7 明确排除在一期之外的 `multipart/alternative` 预留结构，代价是多一个可空列、一条 `CHECK` 与两个 API 字段。
- 把主题与正文的长度上限写成代码常量 —— 每次调值都要重新构建镜像并走一次 tar 交付链。
- 不设长度上限、只靠部署层限制请求体 —— 服务层没有防线，超大正文会入库并留存 30 天且必然投递失败，表现为「提交成功但永远发不出去」。
- `pending_messages` 设 `status` 列（哪怕取值只有 `pending`）—— 常量列不携带信息，却会诱使后来者往里加值（一加值就要处理回收）；状态已由「这一行在哪张表里」表达，再设一列就是第二个真相来源。
- 在 `rejected_messages` 里保留邮件完整原文 —— 复核误判所需的内容调用方手里就有；服务侧多存一份等于按 `NFR-8` 把「被判定不该发出」的内容留存 30 天，使该表成为全库敏感度最高的一张表。
- 把原始的 `In-Reply-To` / `References` 串存进 `messages` —— 解析结果已经落成 `parent_id` 外键，再留一份外部可控的原始串等于给出第二个真相来源（与拒绝保存回复自身 `Message-ID` 同一条理由）。
- 用 PostgreSQL 原生 `ENUM` 类型承载受控枚举 —— 改取值要 `ALTER TYPE`，Alembic autogenerate 对 enum 支持差；取值集合小且稳定，`CHECK` 约束已足够。

### 状态与重试
- 引入「发送中 / 重试中」这类**需要回收的中间态**（已认领、结果未提交、必须靠超时兜回） —— 崩溃后卡在这种状态的行必须靠额外的超时回收机制兜回，否则该邮件永久卡死；换来的是可读性，而恢复口径本来就已经是「状态字段 + `next_attempt_at`」。**注意这条否决的对象是「需要回收的中间态」，不是 `sending`**：`sending` 是已提交的显式等待态（等下一次认领），外部可观测、崩溃后由 `next_attempt_at` 决定何时被重新认领，不需要任何回收机制（结论见 §2.3）。
- 状态列只留终态、其余用 `sent_at IS NULL` + `next_attempt_at` 推断 —— 「从未被接受」的 `failed` 行 `sent_at` 同样是 NULL，每条抢占 SQL 都要再补条件排除终态失败，同一套判断摊到多处、出错面更大。
- 不看返回码、一律重试 3 次 —— 对「用户不存在」这类确定性失败白跑三次、最多浪费 105 秒与三次 SMTP 连接，而调用方在这段时间里看到的是「尚未失败」。
- 维护一张「确定性拒绝」的 SMTP 码表（`550` / `551` / …）来判定永久失败 —— 等于靠猜对端实现，清单必然不完备；SMTP 本身已经用 4xx / 5xx 表达了「暂时 / 永久」的分工。
- 让「投递中」由 `messages.status = sending` **派生**、不在库内落一个显式状态 —— **已推翻**：库内与对外分两套词就要多一层映射，且 `sending` 无法由 `sent_at IS NULL` 推断（该条件同时涵盖 `submitted` / `reviewing` 与「从未被接受的 `failed`」）；库内直接落 `sending` 后，六态是同一套取值，第 4 次尝试后崩溃的遗留行仍由收割支推进终态，不靠任何中间态兜回（结论见 §2.1 落点表与 §2.3）。
- 进入终态失败后主动通知调用方（回调 / 推送）—— 与 `NFR-3`「不做回调推送」及 `spec.md` §7「不主动回调调用方」直接冲突；调用方轮询即可。

### 事务与认领
- 单事务到底（认领 + 审查 / 发信调用 + 写结果放在同一个事务里）—— 数据库事务要跨越一次网络调用（SMTP 超时可达数十秒），期间同批的行全被锁住；且未提交的中间状态在外部不可观测，「审查中」无法兑现，只能回头删掉 `FR-11` 里的那个状态。
- 用 `claimed_at` 之类的专用列表达「进行中」—— 与 `attempt_count` 语义重复，两处都在表达「被认领过」，读表的人要同时理解两者。
- 为 `pending_messages` 的认领条件建索引（原结论：小表顺序扫描已够）—— **已推翻**：`pending_messages` 只承载在审队列、过审即删行，但「积压出现的时刻」正是系统最忙的时刻，那时再加索引等于一次 DDL 加一次迁移；首版即建部分索引 `(direction, next_attempt_at) WHERE attempt_count <= 4` 只多一条索引的写入成本（结论见 §2.2）。
- 用数据库触发器强制状态迁移 —— 迁移合法性由带谓词的条件 UPDATE 保证（只允许声明的起点、`rowcount = 0` 按幂等忽略）即可；把迁移规则放进数据库与「判定要能脱离数据库单测、业务规则不只在 SQL 里」的取向冲突，且触发器会让测试库与恢复流程都多一层隐式行为。

### 游标与幂等
- 把入站回复自身的 `Message-ID` 头值写进 `message_id`，或另开一列专门保留它 —— 该值由收件人侧客户端生成、外部可控，可能重复或畸形；写进唯一列等于让外界能造成写库失败或唯一冲突，而它既不参与关联（关联靠外键）也不参与入站幂等（靠 邮箱 + `UIDVALIDITY` + `UID`）。
- 入站幂等只用 `uidvalidity` + `uid`（省略 `mailbox` 列）—— 与已定的三要素幂等键不一致；将来支持多邮箱必须改唯一约束并迁移，等于把一条不可逆改动提前埋下。
- 入站幂等只记在游标 / 幂等表里、邮件行本身没有唯一约束 —— 判重退回应用层的额外查询，并引入「两个执行体同时判定为新邮件」的竞态；被保护的那条数据反而没有约束兜底。
- 用通用 KV 表承载 IMAP 游标 —— 值没有结构，`uidvalidity` / `last_uid` 的类型与必填只能靠写入方自觉（与错误列否决 jsonb 的理由相同）。
- 不建 `imap_cursors`、用 `messages` 的 `max(uid)` 推断拉取进度 —— UID 允许空洞、`UIDVALIDITY` 会翻转，推断出的进度会静默漏取；且每次拉取都要在热路径上做一次聚合查询。

### 分拣与丢弃
- 关联不上的入站邮件标记为「孤儿」并照样入库 —— 回路只承认「点回复」这一种答复方式，因此不入库的邮件不构成需要保留的数据；代价是「用户不点回复而新写一封」的答复会被丢弃，且丢弃只按原因分类计数，无法从日志定位具体邮件。
- 把入站分拣下沉进 `providers/imap`（适配器直接返回「已分拣好的」回复 / 退信 / 丢弃）—— 业务判定进了适配器，换 IMAP 客户端时要连同分拣一起重写，与「`providers/` 只放外部能力适配、不放业务状态流转」的目录职责冲突。
- 把入站分拣放进 `db/` 仓储层（入库时判定）—— 仓储层只放 SQL 与数据访问；业务规则下沉会让纯判定无法脱离数据库单测。
- 为退信单独加 `bounced_at` 列 —— 一期没有任何消费方会问「退信花了多久」，而「曾发出过」已经由保留的 `sent_at` 表达（`sent_at` 非空 + `status = failed` 即「发出后被退信」）；这条推论的前提是「退信只作用于 `sent_at` 非空的行」（§3.3）。
- 让退信无条件置 `failed`（不区分该行是否已发出）—— `sending` 行收到退信只可能来自「SMTP 已接受、结果事务未提交」的窗口，无条件置终态会打断本可成功的重试，并产出 `sent_at` 为空 + `failed` 这一与「从未被接受」同形的组合；改为按 `dsn_target_not_sent` 计数丢弃（结论见 §3.3）。
- 把退信本身作为一条独立记录入库供调用方查询 —— 要扩展查询契约（`GET .../replies` 是否包含退信），而 DSN 会内嵌原始邮件全文，入库就必须再决定存什么，与 `rejected_messages` 上「不存原文」的取向相抵。
- 把丢弃的入站邮件明细落库供排查 —— 与「关联不上的入站邮件一律不入库」的结论冲突，等于把该结论改成「照样入库」，而明细里必然带着外部可控的头值与内容。
- 丢弃只留一个总数、不做分类计数 —— 「用户一直没收到回复」时无法判断是「没拉回来」还是「拉回来被丢弃」，而分类计数并不越过日志红线。

### 清理
- 为回复另开第 4 张物理表 —— 与「邮件数据表 3 张（全库 4 张）」的结论冲突，且 `NFR-8` 的清理变成跨两张表。
- 待审行过审后只标记状态、保留在表里 —— `pending_messages` 随邮件量单调增长、邮件内容在两表各存一份，`NFR-8` 的清理必须同样覆盖它；而一期审查恒通过、重试语义已固定，完整重试历史几乎没有兑现机会。
- 让保留期清理无差别删除出站行 —— `pending_messages` 不参与清理，父行先被删会留下指向它的入站待审行，流④ 随后写回复行即撞 `parent_id` 外键、把用户的正常回复误报成拒审；清理因此要排除仍有入站待审行指向的出站行（结论见 §3.6）。
- 把「父行已被清理」的后果写成拒审或静默忽略 —— 外键违例按 `reply_target_gone` 计数丢弃，既不写 `rejected_messages`，也不假装成功（结论见 §3.5）。

### 发件人身份与启动校验
- 启动只在日志里告警、继续运行 —— 告警会被忽略，服务会带着静默断掉的回复回路继续接受请求；详见 §4.4。
- 一致性仅靠人工保证（不做校验）—— 又一条「删掉也不会有测试失败、只能靠纪律维持」的约束；详见 §4.4。
- 让被拉取信箱地址默认取 `IMAP_USERNAME` 以省一个配置项 —— 把回路重新变成「两个字段恰好相等」的隐式依赖；详见 §4.4。
- 不写 `Reply-To`（只靠 `From`）—— 回路隐式依赖 `From` 与被拉取信箱恰好一致，改 `From` 即静默断掉；详见 §4.3。
- 允许调用方指定 `From` —— 可伪造任意发件人、服务方无法背书，也破坏可观测性；详见 §4.3。
- 允许调用方指定 `Reply-To` —— 同上，且回复会落到不可控地址；详见 §4.3。

### 审查接口
- 审查出错用尽重试后把邮件置为 `failed` 放进 `messages` —— `failed` 在 `FR-5` / `US-4` 里指投递失败，用它表达「审查器故障」会把系统故障误报成邮件系统故障，而这封邮件根本没到 SMTP。
- 审查出错的行留在 `pending_messages` 里标终态 —— `pending_messages` 不再是纯队列，必须新增 `status` 列与第二套清理逻辑，把「过审即删行」的收益还回去。
- 把拒审原因 / 来源通过 API 暴露给调用方 —— 与「对外只有一个拒审终局」的口径不一致，还要给契约加字段；来源与错误分类留在库内供复核即可。

### 依赖选型
- 用 `aioimaplib` 作为 IMAP 客户端 —— `license = "GPL-3.0"`，与「私有 + MIT、制品不发布、避免许可摩擦」的取向冲突（`docs/research/06-naming-licensing-and-governance.md` 已用同类理由否决 AGPL）；且它自述是把 `imaplib` 原样搬到 asyncio，仍是低层响应模型。
- 用标准库 `imaplib` 自己解析 IMAP 响应 —— `imaplib` 的响应几乎未经解析，`FETCH` / `SEARCH` 响应与 UID 语义要自己写（`IMAPClient` 官方 README 明说这条路会变成「自己那套脆弱的解析」），而分拣正确性是 `AC-11` / `AC-15` 的考点。
- 用 `psycopg3` 作为数据库驱动 —— 能力与 `asyncpg` 等价（本项目「每 10 秒一批」的量级下性能差异不可见），但许可为 LGPL-3.0-only，是两者之间唯一无法用本地纪律化解的代价；`py.typed` 带来的便利不值这一项。
- 为发信引入 `aiosmtplib` —— 新增运行时依赖，而标准库 `smtplib` 配 `asyncio.to_thread` 已足够（与 IMAP 侧同构），隔离环境首次启动与许可核实都不必承担。

## 7. 落地位置
分层与依赖方向见 `docs/architecture.md`；本节只记各设计结论落在哪个目录。

`src/vs_email_forwarding/` 下：

- `flows/` —— 定时任务的入口与编排：四条业务流与保留期清理流的循环与调度器（目录已建，仅有占位文件）。
- `service/` —— 用例层：同步请求路径的业务编排（提交邮件、查询状态与回复），并负责把仓储交回的 ORM 对象转成 `schemas/` 的模型（§8）；目录已建，仅有占位文件。
- `core/` —— 纯逻辑（消息 ID 生成、状态机、重试策略），不碰 I/O 与数据库（目录已建，仅有占位文件）。
- `db/` —— `pending_messages` / `messages` / `rejected_messages` / `imap_cursors` 四张表的 ORM 模型、引擎与会话、仓储层（所有 SQL 都在这；目录已建，仅有占位文件）；引擎与会话的生命周期见 `docs/architecture.md` §5，交给 `service/` 与 `api/` 的模型边界见 §8。
- `providers/` —— `smtp/`、`imap/`、`review/` 适配器，各自以 `base.py` 定接口；业务流程不得直接 import 具体实现库。三层分拣落点见 §3.4。
- `providers/review/` —— 审查供应商接口（`base.py`：中性输入对象 + `pass` / `reject` 受控枚举）与空实现的落点；必须在此标注「空实现、永远通过」（`NFR-6`）；实现由工厂函数按配置选取（见 §4.2）。
- `providers/smtp/` 与 `providers/imap/` —— 发信适配器（`From` 取自配置、显式写入 `Reply-To`）与入站拉取适配器（拉取所用的信箱地址即 `Reply-To` 的取值来源）。
- `api/` —— HTTP 接口层（请求校验、响应序列化、依赖注入），业务编排在 `service/`；鉴权位置预留的**形态**定为 router 级依赖（在 `api_router` 上挂一个当前为空的依赖，后续替换成真实校验即可、不重排路由结构），「何时补、用什么方式」仍未定（`docs/open-questions.md` F3）；`/healthz` 只表达进程可用（§4.1）。
- `config.py` —— 唯一的配置入口（pydantic-settings），四条业务流间隔字段与保留期清理间隔的默认值为 10 / 3600（`NFR-2`）；每批条数、主题与正文长度上限、被拉取信箱地址、IMAP 是否 STARTTLS、三个外部调用超时与回复分页条数都收敛在这一层（见下面的配置项清单）。启动即校验的**判据**也在这层（`validate_startup`），**调用点**在 `main.py` 的 lifespan 显式启动步骤（§4.4）。

仓库其他位置：

- `docs/api-contract.yaml` —— **对外 HTTP 接口契约的唯一真源**（OpenAPI 3.1）：三个端点的路径、请求与响应字段、类型、必填、取值枚举、取值约束（长度上限、地址格式、UUID 格式）与错误响应全在这份文件里。`design.md` §4.1 只保留设计口径（为什么用 keyset、为什么不用状态码区分业务语义、时间格式为什么固定六位小数）与物理落点，**不重复任何字段定义**。
- `.env.example` —— 配置项的占位模板（清单见下表）；仓库只提交这一份；已定名但尚未落进去的那几项见下表的定名状态列。
- `migrations/` —— Alembic 迁移入口（物理表名与字段清单已定：`pending_messages` / `messages` / `rejected_messages` / `imap_cursors`，首次迁移可以动笔）。

### 配置项清单
| 配置项 | 用途 | 定名状态 |
|---|---|---|
| `POLL_REVIEW_OUTBOUND_SECONDS` / `POLL_SEND_SECONDS` / `POLL_FETCH_INBOUND_SECONDS` / `POLL_REVIEW_INBOUND_SECONDS` | 流① / 流② / 流③ / 流④ 的轮询间隔（各默认 10） | 已定（仓库已有） |
| `POLL_CLEANUP_SECONDS` | 流⑤ 保留期清理的执行间隔（默认 3600） | 已定（仓库已有） |
| `SMTP_TIMEOUT_SECONDS` / `IMAP_TIMEOUT_SECONDS` / `REVIEW_TIMEOUT_SECONDS` | 单次外部调用的墙钟上界（各默认 10 秒）；`FR-15` 的 3 分钟由它们与退避表共同推出 | 已定（仓库已有） |
| `REPLIES_PAGE_SIZE` | `GET .../replies` 每页条数上限（默认 100） | 已定（仓库已有） |
| `BATCH_SIZE` | 每批条数，五条流共用（默认 100）；流③ 也用它限制单个 tick 处理封数 | 已定（仓库已有） |
| `MAX_SUBJECT_CHARS` | 主题长度上限，API 层校验，超限返回 422 且不落库（默认 500，单位是字符） | 已定（仓库已有） |
| `MAX_BODY_BYTES` | 正文长度上限，API 层校验，超限返回 422 且不落库（默认 1048576 = 1 MiB，单位是字节） | 已定（仓库已有） |
| `MAIL_FROM_ADDRESS` | 发信 `From` 的唯一来源；其域也用于生成 `Message-ID` | 已定（仓库已有） |
| `REPLY_TO_ADDRESS` | 服务显式写入的 `Reply-To`，必须等于被拉取信箱地址 | 已定（仓库已有） |
| `IMAP_ADDRESS` | 被拉取信箱地址（地址，不是信箱文件夹）：与 `REPLY_TO_ADDRESS` 启动即校验，为空或不一致都拒绝启动 | 已定（仓库已有；字段默认空串，空值由启动校验拒绝） |
| `IMAP_STARTTLS` | IMAP 是否启用 STARTTLS（布尔，默认 true） | 已定（仓库已有） |
| `IMAP_HOST` / `IMAP_PORT` / `IMAP_PASSWORD` | IMAP 主机、端口、口令 | 已定（仓库已有） |
| `IMAP_USERNAME` | IMAP 登录名；不作为被拉取信箱地址的默认值 | 已定（仓库已有） |
| `IMAP_MAILBOX` | 信箱文件夹：入站回复行 `mailbox` 列的取值来源（见 §2.1） | 已定（仓库已有） |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD` / `SMTP_STARTTLS` | SMTP 主机、端口、账号、口令、是否 STARTTLS | 已定（仓库已有） |
| `DATABASE_URL` | 异步驱动连接串，形如 `postgresql+asyncpg://…` | 已定（仓库已有） |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | compose 里 postgres 容器的初始化参数，须与 `DATABASE_URL` 一致 | 已定（仓库已有） |

## 8. 会话边界与模型转换
分层与依赖方向见 `docs/architecture.md`；本节只定「ORM 对象到哪儿为止」这一条，落点是 `db/`、`service/` 与 `api/` 的函数签名。

- **`service/` 只交出 Pydantic / 中性对象**：`db/` 的 ORM 对象不出 `db/` 层——仓储函数把 ORM 行交给 `service/`，`service/` 在自身作用域内立即转换为 `schemas/` 的响应模型或中性对象，再交给 `api/`。
- **`api/` 不依赖 `db/`**：`api/` 的函数签名里不出现会话、ORM 模型或 SQLAlchemy 类型，它只认 `schemas/` 与 `service/`（`docs/architecture.md` §2 的依赖方向由此在类型层面成立，而不只是约定）。
- **转换落在 `service/` 而不是 `api/`**：`api/` 只做请求校验与响应序列化；`service/` 已经掌握事务边界，ORM 相关的读取（含需要访问的字段）都在它自己的会话有效期内完成，不存在「会话已关、`api/` 再去触发懒加载」的路径。
- **`flows/` 同样不越界**：它只消费仓储返回的中性对象与自身需要的字段，不把 ORM 行传到别的层或别的 tick。

### 已否决方向
- **`service/` 直接把 ORM 对象交给 `api/`** —— `api/` 会因此依赖 `db/` 的模型类型，且 ORM 实例与会话绑定：会话一关，任何懒加载或过期字段访问都会在接口层抛出，错误位置与责任层错位。省下的那一次转换，代价是把会话生命周期泄漏到最外层。
- **在 `db/` 仓储层直接返回 Pydantic 模型** —— `db/` 会反向依赖 `schemas/`，破坏「`db/` 不依赖任何业务层」这条硬规则，也让「`schemas/` 只被 `api/` 用」不再成立。

## 9. 未定项
本特性的未定项与全仓未定项的唯一清单都在 `docs/open-questions.md`；本文件不复述任何条目。
