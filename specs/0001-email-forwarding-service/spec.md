# 内部邮件中转服务 规格

| 项 | 值 |
|---|---|
| spec 编号 | `0001` |
| 状态 | `已确认` |
| 负责人 | 所有者 |
| 关联决策记录 | `specs/0001-email-forwarding-service/design.md`（本特性的设计真源）、`docs/research/02-runtime-and-scheduling.md`、`docs/research/03-dependencies-and-toolchain.md`、`docs/research/04-security-and-review.md` |
| 关联 PR | 待补 |

> 本规格只写**需求**：要什么、给谁用、怎样算做完。
> 表结构、接口字段契约、状态取值、驱动与客户端选型等**设计细节不在本规格内**，见 §8 与 `specs/0001-email-forwarding-service/design.md`（本特性的设计真源）；跨特性底座见 `docs/research/` 下各主题册。

## 1. 背景与目标

内网 agent 会话**无法直接与企微通信**。这造成两个后果：

1. agent 停在「等用户回答」时，用户在企微里**感知不到**自己需要做任何事；
2. 用户回答 agent 的方式实际是**回复邮件**。

本服务就是这条问答闭环的桥：agent 把要问的话交给本服务，本服务通过内部邮件系统把一封工作邮件发给指定的内部同事；同事收到后回复邮件（可直接在邮件客户端回复，企业邮箱已与企微打通，企微会提示有新邮件且可在其中回复，回复落到同一只邮箱）；本服务把回复拉回并供 agent 查询。

**回路只承认「点回复」这一种答复方式**：从专用回复邮箱拉回的邮件里，只有**能关联回某封出站邮件的用户回复**，以及**能关联回某封出站邮件的退信**才入库；其余一律**不入库**，只在日志里记一条丢弃条数。因此用户若**不点「回复」而新写一封**，那封答复不会被本服务收录——这是明确接受的代价（见 §7）。

**做完之后什么会变好**：agent 与内部用户之间形成一条可自动化的问答回路，不需要把 agent 直接接进企微，也不需要人工转达。

**两种「完成」必须分清**（这是本规格最重要的一句话）：

- **本服务的完成** = 邮件被内部邮件系统接受投递，且用户之后发回的回复能被服务拉回并供调用方查询；
- **不是本服务的完成** = 用户真的答复了。用户不回、回得晚、回得不对，均由调用方自行判断，服务不代管（见 `FR-15`、§7）。

一期尚**不具备任何真实内容审查能力**（审查为永远通过的空实现，见 `FR-3`），因此本服务在真实审查接入前**不得被当作内容安全边界使用**。

**本规格是服务级能力基线，只此一份**：它覆盖整个服务当前要求提供的能力。后续新增能力（例如接入真实审查、接入 API 鉴权）**各自另开 `specs/0002-*`、`0003-*`**，不在本文件里扩写；本文件只保留仍然有效的服务级要求。

## 2. 术语

| 术语 | 含义 |
|---|---|
| 调用方 | 内网 agent 会话，本服务唯一的程序化使用方；通过 HTTP API 提交邮件、查询状态与回复 |
| 收件人 | 收到问题邮件的内部同事；同时是回复方 |
| 出站邮件 | 由调用方提交、经本服务发给收件人的邮件 |
| 入站邮件 | 本服务从专用回复邮箱拉回的邮件，包含用户回复、退信与阅读回执 |
| 回复 | 入站邮件中靠 `In-Reply-To` / `References` 关联回某封出站邮件的用户答复；只有这类才入库 |
| 退信 | 投递失败后由邮件系统退回专用回复邮箱的**结构化通知**（DSN，见下）；是「投递失败」的信号，不是用户的答复 |
| 阅读回执 | 收件人客户端发出的已读通知（MDN）；不可靠、不保证到达，**不作为任何判据**，识别后即丢弃 |
| 未入库邮件 | 拉回来但既不是「关联上的回复」、也不是「关联上的退信」的邮件；**一律丢弃**，只在日志留条数（见 §7） |
| 审查 | 出站投递前与入站回收入库前都要经过的安全检查环节（一期为永远通过的空实现） |
| 拒审 | 审查判定不通过；该邮件不进入投递，也不作为可交付结果 |
| 专用回复邮箱 | 本服务用 IMAP 拉取回复的那一只信箱；也是服务写入 `Reply-To` 的取值来源 |
| 回复地址 | 服务显式写入的 `Reply-To`，等于专用回复邮箱 |

## 3. 用户故事

- `US-1` 作为**调用方（agent 会话）**，我希望把一段内容发给某个内部同事，以便在无法与企微通信的情况下也能向人提问。
- `US-2` 作为**调用方**，我希望随时能查回这封邮件的回复，以便把用户的答复接回会话继续干活。
- `US-3` 作为**调用方**，我希望查询回复可以重复执行且结果稳定，以便在网络抖动或进程重启后重试而不会看到重复或乱序的回复。
- `US-4` 作为**调用方**，我希望知道一封邮件到底发出去了没有、是不是失败了，以便判断该继续等还是该换个方式联系用户。
- `US-5` 作为**收件人**，我希望收到的是一封普通的内部工作邮件，直接回复即可，不需要安装或学习任何新东西。
- `US-6` 作为**服务所有者**，我希望发出的邮件不可能被伪造发件人，回复必定回到本服务可拉取的信箱，以便这条回路不被人滥用、也不会莫名断掉。
- `US-7` 作为**服务所有者**，我希望未经审查的内容不会被发出去，且审查能力可以在不改动调用方的前提下升级，以便安全能力可以独立演进。
- `US-8` 作为**运维**，我希望服务在无人值守下持续运行、重启后能自行恢复，并且能判断它是否还活着，以便它可以在内网长期挂着。
- `US-9` 作为**服务所有者**，我希望邮件与回复不会无限期堆在库里，以便控制体积与合规面。

## 4. 功能需求

- `FR-1` 接收投递请求：服务提供接口接收调用方提交的一封邮件，必含**单个收件人**、主题、正文（纯文本或 HTML）；一期不支持附件、抄送密送、自定义邮件头（见 §7）。
- `FR-2` 固定发件人身份：发件人取服务配置、服务端显式写入回复地址等于专用回复邮箱；调用方指定发件人或回复地址必须被忽略，绝不生效。
- `FR-3` 投递前审查：每封待发邮件在投递前必须经过审查；审查不通过则**不得投递**并进入拒审路径。一期审查为「永远通过」的空实现，只提供接口与输出形状，且必须被显式标注为空实现。
- `FR-4` 自动投递：审查通过的邮件由后台自动投递，不需要人工触发。
- `FR-5` 投递失败重试与终态：投递失败按固定语义重试，**最多 3 次重试**（首次尝试 + 3 次重试，合计最多 4 次尝试），退避 15s / 30s / 60s；超过上限进入终态失败并**记录最后一次错误**。错误只允许记录 **SMTP 返回码**与**永久 / 临时分类**（`FR-6` 与重试判定共用同一分类），**不得记录对方服务器的原始回复文本**（可能回显地址乃至正文）。**一期的「失败」只区分可重试与不可重试两类，不对可重试的失败细分原因**；这两类由 SMTP 返回码判定（`4xx` 与连接 / 超时类算可重试、`5xx` 算不可重试）**已定案**（见 `specs/0001-email-forwarding-service/design.md`）。
- `FR-6` 退信识别：服务必须靠**邮件结构**识别投递失败通知（DSN），而不是靠主题或正文的自由文本猜测。判定依据至少包含 `multipart/report` 容器与 `message/delivery-status` 部分；结构化字段中的永久失败（`Status: 5.x.x`）与临时失败（`Status: 4.x.x`）**都算失败**。识别后从内嵌的原始邮件中取出原邮件标识，把对应出站邮件置为**发送失败**。若退信中取不出原邮件标识，则**丢弃该退信**——这是明确接受的缺失（见 §7）。退信识别只依赖本服务从专用回复邮箱拉回的内容，不引入任何外部系统，也不依赖对方网关一定发送退信。
- `FR-7` 入站拉取：服务定期从专用回复邮箱增量拉取新邮件；既不漏取，也不重复处理同一封邮件。
- `FR-8` 入站审查：拉回的每封**关联上的回复**同样经过审查；审查不通过则进入拒审路径。
- `FR-9` 入站邮件分拣：拉回的每封邮件必须被分拣为三类之一——**关联上的回复（入库）、关联上的退信（用于置发送失败）、以及其余一律不入库**。阅读回执（MDN）属「其余」，必须在入库前被剔除，绝不允许被当成用户回复。**关联不上的入站邮件不入库**，只在日志中记录丢弃条数。
- `FR-10` 回复查询：调用方可按出站邮件 ID 查询该邮件的全部回复，按到达时间**升序**返回，每封回复有稳定标识；**重复查询返回相同的全集与相同的顺序**。回复数量无上限，因此接口必须支持**分页**：用基于「到达时间 + 回复 ID」的**游标**逐页取，逐页取完即得到全集；**新回复到达不得让已经取过的页漏读或重读**（`limit` / `offset` 式偏移分页不满足这一条），每页条数由配置项决定。**「邮件存在但还没有回复」是正常状态**（用户可能还没看、还没回），此时返回空集合即为正确结果；调用方据此等待即可。它与「这个 ID 查不到」（见 `NFR-8`）的区分靠**响应体**里的显式存在性标志，**不靠 HTTP 状态码、也不靠空集合本身**——状态码只表达请求本身是否成功。
- `FR-11` 邮件状态查询：调用方可按出站邮件 ID 查询该邮件当前状态，并区分**已提交 / 审查中 / 拒审 / 投递中 / 已发出 / 发送失败**六种。状态沿「已提交 → 审查中 → 投递中 → 已发出 / 发送失败」**单调推进**（拒审是从「审查中」分出的终局），**不得出现倒退**；「投递中」涵盖「已过审、首次待发」与「投递失败后的重试等待」两种情形。
- `FR-12` 拒审可见：审查不通过的邮件不得被投递，且调用方能感知到该结果。
- `FR-13` 恢复能力：进程重启后，未完成的审查、投递与入站处理必须能自行推进，不需要人工干预。
- `FR-14` 健康检查：服务提供健康检查端点，供部署与运维判断进程是否可用。
- `FR-15` 时效：从调用方提交成功起，**3 分钟内**必须完成投递尝试（成功或进入失败重试），不得无限期停留在待处理状态。

## 5. 非功能需求

- `NFR-1` 幂等与并发安全：四条后台处理环节（出站审查、投递、入站拉取、入站审查）都必须幂等；同一封邮件被并发处理时**恰好投递一次**。抢占必须按 `SELECT ... FOR UPDATE SKIP LOCKED` + 状态字段 + `next_attempt_at` 语义（见 `AGENTS.md` §8）。
- `NFR-2` 间隔可配置：投递与拉取的处理间隔、每批条数必须是配置项，默认 10 秒，代码中不得硬编码（见 `AGENTS.md` §8）。
- `NFR-3` 离线约束：运行期不得依赖任何外部网络。本服务不主动调用任何外部服务，不与企微对接，不做回调推送。
- `NFR-4` 配置与凭据：连接参数、账号口令、发件人与回复地址一律来自配置层；仓库只含占位符（见 `AGENTS.md` §7、§8）。
- `NFR-5` 日志红线：任何日志、异常信息中不得出现邮件正文、收件人地址、账号口令、原始 MIME；定位邮件一律用邮件 ID（见 `AGENTS.md` §8）。
- `NFR-6` 空实现标注：审查的空实现必须在代码注释与文档中显式标注，防止被误解为「系统已有安全审查」（见 `docs/research/04-security-and-review.md`）。
- `NFR-7` 时间口径：应用内部时间一律带时区、按 UTC 存储（见 `AGENTS.md` §7）。
- `NFR-8` 保留期限：邮件及其回复在库中**保留 30 天**——出站邮件自**提交时刻**起算，入站回复自**其落库时刻**起算（回复没有「调用方提交」这一事件）；到期后邮件与它的回复一起从库中移除，其后查询该 ID 得到「查不到」而非报错。清理动作的范围与落点见 `specs/0001-email-forwarding-service/design.md`（调度地位见 `docs/research/02-runtime-and-scheduling.md`），本规格把它登记为一条**约定**，不写成功能需求。
- `NFR-9` 无人值守：服务随时可能被调用方提交（不在特定时段），必须能长时间无人干预运行；不可用时应当明确拒绝请求而不是静默丢弃。
- `NFR-10` 丢弃留痕：被丢弃的入站邮件必须在日志中留下可计数的记录，**只记条数**（可含本次处理的批次计数），**不得记录发件人、收件人地址、主题、正文或 UID 等任何可回溯到具体邮件的字段**（`NFR-5` 是硬边界）。

## 6. 验收标准

| 编号 | 覆盖 | 验收标准（可测试） |
|---|---|---|
| `AC-1` | `FR-1` | `POST /v1/messages` 提交「单个收件人 + 主题 + 纯文本正文」返回 2xx 且响应含该邮件 ID，随后 `GET /v1/messages/{id}` 能查到该 ID。 |
| `AC-2` | `FR-1` | `POST /v1/messages` 缺少收件人时返回 HTTP 422，响应体中出现字段名（对收件人字段），且库中不新增记录。 |
| `AC-3` | `FR-1` | `POST /v1/messages` 请求含两个收件人时返回 HTTP 422，且库中不新增记录。 |
| `AC-4` | `FR-2` | 请求体显式携带发件人字段时，实际投递出的邮件 `From` 仍等于服务配置值，且不会以调用方给的地址发出。 |
| `AC-5` | `FR-2` | 集成测试捕获到的邮件必须含 `Reply-To`，且其值等于专用回复邮箱配置项。 |
| `AC-6` | `FR-3` | 集成测试用「总是拒绝」的假审查实现替换空实现后，该邮件**不产生任何 SMTP 投递**，且其状态为拒审。 |
| `AC-7` | `FR-4` | 真实 PostgreSQL + 本地邮件捕获服务下，执行一次投递环节后捕获端收到该邮件，且库中该邮件状态为已发出。 |
| `AC-8` | `FR-5` | 让假 SMTP 服务器以**临时性错误**（`4xx` / 连接故障）连续报错时，库中该邮件在**第 3 次重试用尽时**（即第 4 次尝试失败后）进入终态失败，且记录了最后一次错误的 **SMTP 返回码与永久/临时分类**。 |
| `AC-9` | `FR-5` | 三次重试的间隔符合 15s / 30s / 60s：断言每次重试的 `next_attempt_at` 与上次尝试时间的差值分别约为 15、30、60 秒。 |
| `AC-10` | `FR-6` | 向专用回复邮箱注入一封 DSN（`multipart/report` 且含 `message/delivery-status`，内嵌原邮件）后，运行一次入站环节，其内嵌原邮件对应的出站邮件状态变为发送失败。 |
| `AC-11` | `FR-7` | 同一封入站邮件在 mailbox + UIDVALIDITY + UID 不变的情况下连续拉取两次，库中只有一条记录，且第二次不再重复入库。 |
| `AC-12` | `FR-8` | 用「总是拒绝」的假审查实现时，拉回的入站邮件**不进入调用方可查询到的回复集合**，且其后 `GET .../replies` 不返回它。 |
| `AC-13` | `FR-9` | 注入一封与任何出站邮件都关联不上的入站邮件时，**库中不新增任何记录**；该次处理产生一条只含条数的丢弃日志记录。 |
| `AC-14` | `FR-9` | 注入一封阅读回执（含 `message/disposition-notification`）与一封带 `In-Reply-To` 指向某出站邮件的普通回复，运行一次入站环节后：回执不入库、且该出站邮件的 `GET .../replies` 只返回那 1 条回复。 |
| `AC-15` | `FR-10` | `GET /v1/messages/{id}/replies` 连续调用两次，两次返回的回复 ID 集合与顺序完全一致。 |
| `AC-16` | `FR-10` | 某出站邮件收到 3 封回复后，该接口返回 3 条，且按到达时间升序排列。 |
| `AC-17` | `FR-11`、`NFR-7` | 提交一封邮件后**立即**查询得「已提交」，执行一次投递环节后得「已发出」，再注入一封关联回它的退信并执行一次入站环节后得「发送失败」——三个阶段 `GET /v1/messages/{id}` 返回的状态取值互不相同；响应中的时间字段为带时区时间戳，且与库中 UTC 存储值一致。 |
| `AC-18` | `FR-12` | 被拒审的邮件不出现在已发出状态中，且 `GET /v1/messages/{id}` 对该 ID 返回拒审状态。 |
| `AC-19` | `FR-13`、`NFR-9` | 在有待处理邮件时重启进程，重启后不依赖人工干预，该邮件在 3 分钟内被发出。 |
| `AC-20` | `FR-14`、`NFR-3` | 在与公网隔离、可达内网镜像源的服务器上启动容器（运行时不依赖任何外部网络），60 秒内 `GET /healthz` 返回 200。 |
| `AC-21` | `FR-15` | 提交一封邮件后不干预，集成测试在 3 分钟内断言该邮件已进入已发出或失败重试状态。 |
| `AC-22` | `NFR-8` | 库中直接写入 `created_at` 为 31 天前的邮件与其回复各一条、以及一条 31 天前的拒审行，清理动作执行一次后这三条均查不到，而 29 天前的记录仍在；另写入一条 31 天前、但仍有入站待审行指向它的出站邮件，清理后该出站行**仍在**（不得把待审行留成指向已删行的孤儿）。 |
| `AC-23` | `NFR-2` | 把投递间隔配置改为非 10 秒的值后（不重新构建镜像），服务**读取并使用的投递间隔**随该配置改变：断言「调度器构造时读到的间隔 == 配置值」，改配置后重新构造即为新值；另有静态检查确认间隔与批量不以字面量出现在源码中。 |
| `AC-24` | `NFR-5` | 在投递一封含可识别正文与收件人地址的邮件后，采集全部日志，断言日志文本中不出现该正文片段与收件人地址。 |
| `AC-25` | `NFR-6` | 审查供应商接口文件与空实现文件的开头注释中均出现「空实现」与「永远通过」字样；`docs/security.md` 的安全边界一节能通过关键词检索到该标注。 |
| `AC-26` | `NFR-1` | 构建期静态检查：源码中不存在「先 `SELECT` 判断、再 `UPDATE`」的两步抢占写法（所有抢占语句同时含 `FOR UPDATE SKIP LOCKED`）。 |
| `AC-27` | `NFR-4` | `git ls-files` 结果中包含 `.env.example` 但不包含 `.env`；代码检索确认没有模块直接读环境变量，连接参数与地址只经由配置层读取。 |
| `AC-28` | `NFR-10` | 连续丢弃 3 封关联不上的入站邮件后，采集日志：存在一条条数记录（合计 3），且日志文本中不出现任何发件人地址、主题或 UID。 |
| `AC-29` | `NFR-1`、`FR-4` | 同一封出站邮件在库中**恰好一次** SMTP 投递：两个并发执行体同时抢占同一批待发邮件时，捕获端只收到一封。 |
| `AC-30` | `FR-5` | 让假 SMTP 服务器返回一段含收件人地址的原始回复文本，投递并进入终态失败后：库中错误内容只含 SMTP 返回码与永久/临时分类，**不出现那段原始文本、也不出现收件人地址**。 |
| `AC-31` | `FR-10` | 对一封存在但尚无回复的邮件调用 `GET /v1/messages/{id}/replies`：返回空集合，且 HTTP 状态为成功——「未回复」是正常结果，不是错误。 |
| `AC-32` | `FR-2`、`NFR-4` | 三条启动校验各自单独制造一次违规：①「被拉取信箱地址」与 `REPLY_TO_ADDRESS` 不同；② `MAIL_FROM_ADDRESS` 取不到邮件域（如留空）；③ `REPLY_TO_ADDRESS` 不是合法地址——每种情况下进程都**拒绝启动**（非零退出），且不开始监听 HTTP 端口。 |
| `AC-33` | `FR-1` | `POST /v1/messages` 提交主题超过长度上限（或正文超过上限）的请求返回 HTTP 422，且库中不新增记录；上限取值来自配置项，不来自代码常量。 |
| `AC-34` | `FR-5` | 让假 SMTP 服务器对同一封邮件持续返回 `5xx`：**第一次**投递尝试后该邮件即进入终态失败（`attempt_count` 为 1，不出现退避等待），且错误分类记为永久（`permanent`）。 |
| `AC-35` | `FR-10`、`NFR-8` | 用一个不存在的 ID 调用 `GET /v1/messages/{id}` 与 `GET /v1/messages/{id}/replies`：两者都返回成功状态码，响应体显式标识该 ID 不存在，且与「存在但尚无回复」（成功状态 + 空集合）可区分。 |
| `AC-36` | `FR-11` | 提交一封邮件后高频轮询 `GET /v1/messages/{id}` 直到出现「已发出」：期间出现的取值序列**不得包含任何向后步骤**（出现「投递中」之后再出现「已提交」或「审查中」即失败），且至少观测到「审查中」与「投递中」两个取值。 |
| `AC-37` | `FR-5`、`NFR-1` | 构造「第 4 次尝试已被认领、结果尚未写库」的遗留状态（`attempt_count` 为 4、状态为「投递中」、`next_attempt_at` 已到点）后运行一次投递环节：该邮件进入终态「发送失败」（不得永久停在「投递中」），且捕获端**没有**收到第 5 次投递。重启后的自动恢复由 `AC-19` 覆盖——本条的判定对象是收割支，不是进程生命周期。 |
| `AC-38` | `FR-6` | 对一封仍在重试等待中的邮件（`sending`、`sent_at` 为空）注入一封关联它的 DSN 并运行一次入站环节：该邮件**不变成发送失败**、重试照常推进，且本次处理产生一条 `dsn_target_not_sent` 计数。 |
| `AC-39` | `FR-7`、`NFR-1` | 把入站游标回退到已处理过的 UID 后运行一次入站环节：库中不产生第二条记录、游标**仍然推进**（不因唯一键冲突卡住），且本次处理计入一条 `duplicate`。 |
| `AC-40` | `FR-15` | 让假 SMTP 服务器（或假审查实现）接受连接后挂住不返回：单次尝试在配置的超时值内结束、不无限期占用执行体，且该邮件仍能在 3 分钟内进入「已发出」或「发送失败」。 |
| `AC-41` | `FR-10` | 某出站邮件有 250 条回复、每页 100 条：用 `next_cursor` 逐页取完得到 250 条且顺序与全集一致；在翻页过程中插入一条新回复，已取过的页不重不漏，继续翻页最终仍取到全集。 |

## 7. 明确的非目标

- **不提供附件能力**：一期不支持附件（`specs/0001-email-forwarding-service/design.md`）。
- **不支持抄送、密送、自定义邮件头**：一期只有单个收件人、主题与正文（`specs/0001-email-forwarding-service/design.md`）。
- **不提供模板与变量替换**：邮件内容完全由调用方给定，服务只负责发出，不生成也不拼装内容。
- **不接入真实审查能力**：一期只做「永远通过」的空实现；敏感信息检测、病毒扫描、外部审查服务都不做。因此**本服务在一期不得被当作内容安全边界使用**。
- **不实现 API 鉴权**：服务仅内网可达，一期不做鉴权；但必须在 API 层预留鉴权位置，后续接入不重排路由结构。
- **不对接企微**：服务不向企微推送、不读取企微消息，也不主动回调调用方。企业邮箱与企微的联动由邮件系统与企微之间的既有能力完成，不属于本服务。
- **不代管「用户回不回」**：服务不判断「多久没人回复算失败」，也不自动重发提醒邮件。
- **不提供人工审核环节**：审查与投递全自动，没有「等人点批准」这一环。
- **不做消息撤回**：已提交的邮件不能撤回、不能取消。
- **不做同一封邮件的自动去重**：调用方提交两次就是两封独立邮件，各自跟踪，服务不去判断「内容是否相同」。
- **不做多实例部署**：一期单实例；但数据访问层从第一天就按并发安全写（`docs/research/02-runtime-and-scheduling.md`）。
- **不提供面向收件人的任何界面**：收件人只用普通邮件客户端。
- **不收录「不点回复而新写一封」的答复**：这类邮件关联不上原出站邮件，按 `FR-9` 一律丢弃。这是明确接受的代价——**回路只承认「点回复」**。若日后要支持，属**需求变更**（要重新定义「什么算答复」），需重新评估后再改本规格。
- **不兜底「取不出原邮件标识的退信」**：DSN 依赖对方网关内嵌原邮件才能关联；网关丢弃或改写内嵌原文时，该退信被丢弃，**调用方不会因此看到发送失败**。退信本身也不保证一定到达，因此**退信检测是尽力而为，不作为「是否送达」的唯一判据**。
- **不实现任何送达确认**：不请求、不依赖、不解析已读回执（MDN）；这类回执识别后即丢弃，绝不当作回复。
- **不对入站邮件做内容审查之外的分类识别**：不识别自动回复（休假、外出、离职）、不识别邮件列表与营销邮件；它们无法关联时即被丢弃。

## 8. 设计阶段条目（已全部拍板）

需求层面**已无未决问题**；本节登记的 `Q-20`～`Q-27` 是设计阶段条目，**不影响需求是否成立**，但会阻塞实现——它们**已逐条拍板，结论写进 `specs/0001-email-forwarding-service/design.md`**（跨特性底座仍写进 `docs/research/` 下对应主题册），因此本节不再保留表格。本节条目与 `docs/open-questions.md` 的对应条目同源。

- `Q-n` 编号从 `Q-20` 起，是因为 `Q-1`～`Q-19` 这一段号在需求讨论中已被占用（其中定案的条目结论已并入 `FR-1`～`FR-15`、`NFR-1`～`NFR-10` 与 §1，不再单独罗列）；**编号不连续不等于条目缺失**。
- **本节当前为空：`Q-20`～`Q-27` 已全部拍板**，结论在本特性的 `specs/0001-email-forwarding-service/design.md`，跨特性底座在 `docs/research/02-runtime-and-scheduling.md`、`docs/research/03-dependencies-and-toolchain.md` 与 `docs/research/04-security-and-review.md`。

**交付范围说明（不是待决问题）**：`spec.md` 已确认，本特性的设计结论已全部落在同目录的 `design.md`（跨特性底座在 `docs/research/` 各册）。按 `AGENTS.md` §5，`plan.md` 与 `tasks.md` 在 spec 确认之后写——本规格确认后即可动笔。

## 9. 验收结果

> 本特性已按 `tasks.md` 阶段 1～5 实现，验收结果逐条回填如下；`AC-20` 属交付阶段验证（隔离环境容器启动），已在 `T-45` 本机交付验证中实测回填（见下表该行）。门禁 `bash scripts/check.sh` 五步全绿——第 1 步 ruff format --check、第 2 步 ruff check、第 3 步 `mypy src`、第 4 步 pytest（含覆盖率）、第 5 步 mutmut——下表每条用例级证据都被第 4 步实际执行。

| 编号 | 验证方式（命令 / 测试名 / 步骤） | 结果 |
|---|---|---|
| `AC-1` | `tests/integration/test_api_contract.py::test_submit_then_query` | 通过 |
| `AC-2` | `tests/integration/test_api_contract.py::test_missing_recipient_422` | 通过 |
| `AC-3` | `tests/integration/test_api_contract.py::test_two_recipients_422` | 通过 |
| `AC-4` | `tests/integration/test_send_flow.py::test_from_fixed_from_config`、`tests/integration/test_api_contract.py::test_caller_sender_fields_are_ignored` | 通过 |
| `AC-5` | `tests/integration/test_send_flow.py::test_reply_to_written` | 通过 |
| `AC-6` | `tests/integration/test_review_outbound_flow.py::test_reject_no_smtp` | 通过 |
| `AC-7` | `tests/integration/test_send_flow.py::test_outbound_end_to_end` | 通过 |
| `AC-8` | `tests/integration/test_send_flow.py::test_transient_exhausts_attempts` | 通过 |
| `AC-9` | `tests/unit/test_retry_policy.py::test_next_attempt_at_first_three_claims_push_backoff`、`tests/integration/test_send_flow.py::test_backoff_written_on_claim` | 通过 |
| `AC-10` | `tests/integration/test_fetch_inbound_flow.py::test_dsn_sets_failed` | 通过 |
| `AC-11` | `tests/integration/test_fetch_inbound_flow.py::test_inbound_dedup_by_uid` | 通过 |
| `AC-12` | `tests/integration/test_review_inbound_flow.py::test_rejected_reply_not_queryable`、`tests/integration/test_fetch_inbound_flow.py::test_rejected_reply_not_queryable` | 通过 |
| `AC-13` | `tests/integration/test_fetch_inbound_flow.py::test_unmatched_inbound_discarded` | 通过 |
| `AC-14` | `tests/integration/test_fetch_inbound_flow.py::test_mdn_dropped_reply_kept` | 通过 |
| `AC-15` | `tests/integration/test_api_contract.py::test_replies_stable` | 通过 |
| `AC-16` | `tests/integration/test_api_contract.py::test_replies_sorted_asc` | 通过 |
| `AC-17` | `tests/integration/test_api_contract.py::test_three_phase_states`、`tests/unit/test_schemas.py::test_status_timestamps_serialize_as_utc_rfc3339_six_digits_z` | 通过 |
| `AC-18` | `tests/integration/test_api_contract.py::test_rejected_state_queryable` | 通过 |
| `AC-19` | `tests/integration/test_send_flow.py::test_restart_resumes` | 通过 |
| `AC-20` | `bash deploy/run.sh` → `GET /healthz` 返回 200（实测 14.65 秒 ≤ 60 秒，隔离形态：镜像架构复检 → 迁移到 head → 起服务 → 探活；运行时零外部网络依赖：观察窗口内已建立出站连接 0） | 通过 |
| `AC-21` | `tests/unit/test_retry_policy.py::test_worst_case_attempts_within_180s`、`tests/integration/test_send_flow.py::test_within_three_minutes` | 通过 |
| `AC-22` | `tests/integration/test_cleanup_flow.py::test_cleanup_scope_and_not_exists` | 通过 |
| `AC-23` | `tests/unit/test_scheduler_intervals.py::test_interval_comes_from_config`、`tests/unit/test_scheduler_intervals.py::test_no_interval_literals` | 通过 |
| `AC-24` | `tests/integration/test_send_flow.py::test_logs_no_content` | 通过 |
| `AC-25` | `tests/unit/test_review_placeholder.py::test_empty_implementation_annotated`、`tests/unit/test_review_placeholder.py::test_security_boundary_section_mentions_annotation` | 通过 |
| `AC-26` | `tests/unit/test_repository_statements.py::test_claim_select_compiles_with_for_update_skip_locked`、`tests/unit/test_repository_statements.py::test_claim_builder_uses_with_for_update_skip_locked` | 通过 |
| `AC-27` | `tests/unit/test_config_hygiene.py::test_only_config_module_reads_environment_variables`、`tests/unit/test_config_hygiene.py::test_only_config_module_reads_dotenv`、`tests/unit/test_config_hygiene.py::test_env_files_are_tracked_correctly` | 通过 |
| `AC-28` | `tests/integration/test_fetch_inbound_flow.py::test_discard_log_only_counts` | 通过 |
| `AC-29` | `tests/integration/test_repositories.py::test_concurrent_claim_single_delivery`、`tests/integration/test_send_flow.py::test_concurrent_claim_single_delivery` | 通过 |
| `AC-30` | `tests/integration/test_send_flow.py::test_error_has_no_free_text` | 通过 |
| `AC-31` | `tests/integration/test_api_contract.py::test_existing_without_replies` | 通过 |
| `AC-32` | `tests/unit/test_settings_validation.py::test_imap_address_must_equal_reply_to`、`tests/unit/test_settings_validation.py::test_mail_from_address_must_yield_a_domain`、`tests/unit/test_settings_validation.py::test_reply_to_address_must_be_valid`、`tests/integration/test_startup_validation.py::test_violation_refuses_to_start_and_does_not_listen` | 通过 |
| `AC-33` | `tests/integration/test_api_contract.py::test_over_limit_422`、`tests/unit/test_request_validation.py::test_submit_subject_over_config_limit_422`、`tests/unit/test_request_validation.py::test_submit_body_over_config_limit_counts_bytes_not_chars` | 通过 |
| `AC-34` | `tests/integration/test_send_flow.py::test_permanent_fails_immediately` | 通过 |
| `AC-35` | `tests/integration/test_api_contract.py::test_not_found_vs_no_replies` | 通过 |
| `AC-36` | `tests/integration/test_api_contract.py::test_state_monotonic` | 通过 |
| `AC-37` | `tests/integration/test_send_flow.py::test_reaper_after_fourth_attempt` | 通过 |
| `AC-38` | `tests/integration/test_fetch_inbound_flow.py::test_dsn_target_not_sent` | 通过 |
| `AC-39` | `tests/integration/test_fetch_inbound_flow.py::test_cursor_rollback_advances`、`tests/integration/test_repositories.py::test_duplicate_insert_does_not_rollback_advance` | 通过 |
| `AC-40` | `tests/integration/test_send_flow.py::test_timeout_bounded` | 通过 |
| `AC-41` | `tests/integration/test_repositories.py::test_keyset_250_replies`、`tests/integration/test_api_contract.py::test_reply_paging_with_250_rows` | 通过 |
