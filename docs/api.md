# 对外接口与用法

> 本文件是**调用方的使用说明**：本服务对外提供哪些 HTTP 接口、怎么调、调用顺序与判读口径。
> **路径、方法、请求与响应字段、类型、必填、取值枚举、错误响应的唯一真源是 `docs/api-contract.yaml`**；
> 本文件只写「怎么用」，**不复述字段定义**。契约与实现的一致性由 `tests/unit/test_openapi_contract.py`
> 逐字段比对锤住（`create_app().openapi()` ↔ 契约），两者不会各说一套。
> 契约变更属不可逆决策（`AGENTS.md` §6）：先改 `docs/api-contract.yaml`，再同步
> `specs/0001-email-forwarding-service/design.md` §4.1 与实现。

---

## 1. 一句话：谁在调、调的是什么

DSH 会话（或任一内网调用方）把要发的邮件**交给本服务**，由服务做安全审查、经 SMTP 投递，并按出站邮件 ID 查回用户的回复。**调用方不直接发邮件。**

| 项 | 值 |
|---|---|
| 基址 | `http://<服务地址>:<HTTP_PORT>`（`HTTP_PORT` 见 `.env`，容器内固定 8000） |
| 协议 | HTTP/1.1；请求与响应均为 `application/json`（UTF-8） |
| 鉴权 | **一期不做鉴权**（服务仅内网可达；鉴权位置已在 API 层预留，形态未定，见 `docs/open-questions.md` 的 F3） |
| 交互方式 | **拉取式**：调用方轮询查询端点，服务不向调用方推送事件、不提供回调 |
| 自省文档 | `GET /docs`（Swagger UI）与 `GET /openapi.json` 可手工试调；**真源仍是 `docs/api-contract.yaml`** |

**发件人不由调用方决定**：`From` 与 `Reply-To` 一律取自服务配置。请求体里出现的同名未知字段**忽略、不报错**（携带发件人字段也不会生效）。

## 2. 端点总表

| 方法 | 路径 | 用途 | 成功码 |
|---|---|---|---|
| `POST` | `/v1/messages` | 提交一封邮件（立即返回，不等投递结果） | 200 |
| `GET` | `/v1/messages/{id}` | 查一封出站邮件的当前状态 | 200 |
| `GET` | `/v1/messages/{id}/replies` | 查一封出站邮件的回复（游标分页） | 200 |
| `GET` | `/healthz` | 健康检查（探活用，业务调用方一般不需要） | 200 / 503 |

三个 messages 端点里的 `{id}` 是**出站邮件 ID**（提交响应返回的 UUID），三者是同一个 ID 空间。

## 3. 通用口径（先读这节，能省掉大半困惑）

1. **HTTP 状态码只表达「请求本身是否成功」，不承载业务语义。**「邮件不存在」是 `200` + `found: false`，「存在但还没有回复」是 `200` + `found: true` + 空数组——**不要用 404 之类的状态码判断业务**。
2. **`422` = 请求本身不合法**（缺必填、地址非法、超长、`id` 不是 UUID、`limit < 1`、游标形状不对……）。校验全部发生在**写库之前**，被拒的请求**不落库**；`422` 的响应体由实现框架生成，契约**不固定其形状**（判读时只看状态码）。
3. **时间是 UTC 的 RFC 3339、固定六位小数 + `Z`**（如 `2030-01-01T00:00:00.000000Z`）：调用方不需要处理本地时区或可变的小数位数。
4. **`POST /v1/messages` 不幂等**：同一次提交发两次就是两封真邮件，服务不提供去重键。**网络超时后不要盲目重试**。
5. **一期边界**：只有单个收件人、主题、正文；**不支持附件、抄送密送、自定义邮件头**；发件人与 `Reply-To` 由服务配置固定。
6. **长度与分页上限是服务配置**（主题 `MAX_SUBJECT_CHARS`、正文 `MAX_BODY_BYTES`、回复分页 `REPLIES_PAGE_SIZE`）：超限是 `422`（分页超上限是**截断**）。当前取值见 `docs/configuration.md`——契约里不写死这些数字。

## 4. 提交一封邮件

`POST /v1/messages`

请求体（字段定义见契约 `SubmitMessageRequest`）：

| 字段 | 必填 | 说明 |
|---|---|---|
| `recipient` | 是 | **单个**收件人地址；数组或逗号分隔的多地址、含空白（含 CR / LF）一律 `422` |
| `subject` | 是 | 非空；含 CR / LF 即 `422`（头注入）；超长 `422` |
| `body` | 是 | 非空；超长（按**字节**算）`422` |
| `body_format` | 否 | `text`（缺省）或 `html` |

```bash
curl -sS -X POST http://<服务地址>:<HTTP_PORT>/v1/messages \
  -H 'Content-Type: application/json' \
  -d '{"recipient":"<收件人地址>","subject":"<主题>","body":"<正文>"}'
```

```json
{"id": "<出站邮件 ID>", "status": "submitted"}
```

要点：

- 返回即「**已受理**」：此时只是一行待审记录，投递由后台流异步完成，**本请求不等待投递结果**；初始 `status` 恒为 `submitted`。
- 拿这个 `id` 去查状态与回复。
- 触发 `422` 的完整清单见契约该端点的 `description`（服务端另有一份逐条断言在 `tests/unit/test_request_validation.py`）。

## 5. 查一封邮件的当前状态

`GET /v1/messages/{id}`

```bash
curl -sS http://<服务地址>:<HTTP_PORT>/v1/messages/<出站邮件 ID>
```

```json
{
  "id": "<出站邮件 ID>",
  "found": true,
  "status": "sent",
  "created_at": "2030-01-01T00:00:00.000000Z",
  "sent_at": "2030-01-01T00:00:05.000000Z",
  "reviewed_at": "2030-01-01T00:00:03.000000Z"
}
```

要点：

- `found` **显式**标识该 ID 是否存在：为 `false` 时 `status` 与尚未发生的事件时刻都是 `null`，`id` 仍回显你查的那个值。
- 六个字段都是**必填字段**（可空 ≠ 可省略）：调用方不必处理「字段缺失」。
- **`{id}` 的 ID 空间只含出站邮件**：拿回复行的 `id`（即 `Reply.id`）来查一律 `found: false`，**不另设错误码**。

**调用方该按状态做什么**（取值枚举的真源是契约，这里只写用法）：

| `status` | 中文名 | 调用方该做什么 |
|---|---|---|
| `submitted` | 已提交 | 继续轮询 |
| `reviewing` | 审查中 | 继续轮询 |
| `sending` | 投递中 | 继续轮询（含投递失败后的重试等待） |
| `sent` | 已发出 | **终态**：停止轮询，转去查回复 |
| `failed` | 发送失败 | **终态**：停止轮询，按失败告知用户（服务已按既定退避重试到上限，语义见 `docs/research/02-runtime-and-scheduling.md`） |
| `rejected` | 拒审 | **终局**：内容安全审查不通过（或审查环节出错用尽重试）；不要重投同一封内容 |

状态**单调推进、不倒退**，可据此轮询收口。轮询间隔不必小于后台轮询流的间隔（相关配置项见 `docs/configuration.md`），几十秒一次足够。

## 6. 查一封邮件的回复

`GET /v1/messages/{id}/replies?limit=<n>&after=<游标>`

```bash
# 第一页
curl -sS 'http://<服务地址>:<HTTP_PORT>/v1/messages/<出站邮件 ID>/replies'
# 下一页：把上一页响应里的 next_cursor 原样传给 after
curl -sS 'http://<服务地址>:<HTTP_PORT>/v1/messages/<出站邮件 ID>/replies?after=<next_cursor>'
```

```json
{
  "id": "<出站邮件 ID>",
  "found": true,
  "replies": [
    {
      "id": "<回复 ID>",
      "subject": "<主题>",
      "body": "<正文>",
      "body_format": "text",
      "received_at": "2030-01-02T00:00:00.000000Z"
    }
  ],
  "next_cursor": "<received_at>,<id>"
}
```

要点：

- 按 `received_at ASC, id ASC` **升序**返回；**重复查询得到相同的全集与相同的顺序**。
- **游标翻页（keyset）**：`next_cursor` 为 `null` 表示已取完；`after` 对调用方是**不透明字符串**，只需把上一页的 `next_cursor` 原样回传，不要自己拼、也不要解析。
- **不要用 `limit` / `offset` 翻页**：新回复到达会让偏移窗口漂移，与「重复查询得到相同全集」冲突。
- `limit` 的缺省与上限都取配置项 `REPLIES_PAGE_SIZE`；**超过上限是截断，不是 422**（`limit < 1` 才是 422）。
- `found: false` = 该 ID 不存在（此时 `replies` 是空集合）；`found: true` + 空 `replies` = **存在但尚无回复，是正常状态、不是错误**。
- `body_format` 告诉你拿到的是纯文本还是 HTML 部件（服务优先取纯文本）。
- **本接口只返回已入库的入站回复**：入站分拣阶段丢弃的邮件根本不入库，入站审查未通过的回复落拒审表、不在这里出现（落点与判据见 `specs/0001-email-forwarding-service/design.md` §2）。

## 7. 健康检查

`GET /healthz`

```json
{"status": "ok", "app_env": "prod", "version": "0.1.0"}
```

- 语义只有「**进程可用** + 一次轻量数据库探测」；数据库探测失败返回 `503`，响应体只有一句「数据库不可用」，不含连接串、凭据或主机名。
- **它不表达五条后台任务是否在跑**；流是否推进看服务日志与独立探针。业务调用方一般不需要这个端点。

## 8. 一条完整调用序列

```bash
BASE=http://<服务地址>:<HTTP_PORT>

# 1) 提交，取回出站邮件 ID
ID=$(curl -sS -X POST "$BASE/v1/messages" -H 'Content-Type: application/json' \
       -d '{"recipient":"<收件人地址>","subject":"<主题>","body":"<正文>"}' \
     | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

# 2) 轮询状态，直到终态（sent / failed / rejected）——示例只演示判读，真实调用方要自己加间隔与次数上限
curl -sS "$BASE/v1/messages/$ID" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'

# 3) 取回复；next_cursor 不为 null 就带着它再请求一次，直到为 null
curl -sS "$BASE/v1/messages/$ID/replies"
```

## 9. 调用方常见的错法

| 错法 | 为什么不对 |
|---|---|
| 用 `404` 判断「邮件不存在」 | 服务不这么做：一律 `200` + `found: false` |
| 把 `found: false` 当错误反复重试 | 它可能只是 ID 写错，或用了回复行的 `id` |
| 拿 `Reply.id` 去查状态 | ID 空间只含出站邮件，一律 `found: false` |
| 把 `422` 当业务拒绝（拒审） | `422` 是**请求本身不合法且未落库**；拒审是 `status: rejected` |
| 用 `limit` / `offset` 翻页 | 本接口是 keyset 游标，`after` 只接受上一页的 `next_cursor` |
| 把 `found: true` + 空 `replies` 当错误 | 那是「还没有人回复」，正常状态 |
| 网络超时后重发同一封 `POST` | POST 不幂等，会多出几封真邮件 |
| 传 `from` / `cc` / 自定义邮件头 | 一期不支持：未知字段被忽略，发件人与 `Reply-To` 由服务配置固定 |
| 等提交响应里给出最终状态 | 提交只返回「已受理」（`submitted`），投递异步完成 |

## 10. 边界

- **字段、枚举、状态码的真源是 `docs/api-contract.yaml`**，本文件不复述定义；两者冲突时**以契约为准**，并提一个修正本文件的改动（`docs/README.md` §4 第 9 条）。
- 服务固定语义（状态取值、重试上限与退避、受控枚举）的落点是 `src/vs_email_forwarding/constants.py`，不随环境变化。
- 本服务对外的接口面**只有上表四个端点**；它向外的 SMTP / IMAP / 审查调用是服务的内部实现，不是调用方的接口。
- 鉴权形态（API Key / mTLS / 内网白名单）尚未决定（`docs/open-questions.md` 的 F3）——接入真实鉴权后，本文件必须同步更新调用方式。
