# 内部邮件中转服务 任务清单

| 项 | 值 |
|---|---|
| spec 编号 | `0001` |
| 对应规格 | `specs/0001-email-forwarding-service/spec.md` |
| 对应计划 | `specs/0001-email-forwarding-service/plan.md` |
| 状态 | `未开始` |

## 使用说明

- 任务**按依赖顺序**排列，不要跳序执行。
- 每个任务行的「前置」**就是任务图的边**：前置未完成，该任务不能开工。阶段划分只是可读性分组。
- **一条任务 = 一个垂直片**：能在实现之前先红的单测与该实现合并在同一条任务里（理由与清单见 `plan.md` §9.1），因此任务总数与阶段区间保持 `T-1` ~ `T-45`。
- 集成测试与跨层静态检查集中在阶段 5，因为它们要等迁移、夹具、真实库与五条流全部到位才写得出。
- 完成一项就改成 `[x]`，提交信息里引用 spec 编号与本任务编号。

---

## 阶段 1：数据模型与迁移

- [x] `T-1` （前置 无）核对 `src/vs_email_forwarding/constants.py` 与 `design.md` §2.3 / §3.4 逐项一致（六态取值与中文名、合法迁移、`CHECK` 取值集合、十一类丢弃原因、拒审来源、`ErrorClass`、`MAX_ATTEMPTS` 与退避表、`4xx` / `5xx` 区间、时间戳格式），确认它零项目内 import，缺项就地补齐 — `FR-11` / `AC-17`
- [x] `T-2` （前置 `T-1`）四张表的 ORM 模型：字段、`CHECK`、六项索引、`parent_id` 自引用外键与级联删除 — `FR-11` / `AC-22`
- [x] `T-3` （前置 `T-1`）异步引擎与会话工厂：进程级单例、按阶段开短会话、退出时 `dispose()` — `FR-13` / `AC-19`
- [x] `T-4` （前置 `T-2`）Alembic 异步迁移外壳（`alembic.ini` 不写连接串、`env.py`、`script.py.mako`）与 `migrations/README.md` 的就地改写 — `FR-11` / `AC-32`
- [x] `T-5` （前置 `T-4`、`T-3`）首版迁移 `0001_create_mail_tables`：四张表 + 六项索引 + 全部 `CHECK` + 一处自引用外键，`upgrade()` / `downgrade()` 都实现 — `FR-11` / `AC-22`

## 阶段 2：仓储层（所有 SQL 只在这层）

- [x] `T-6` （前置 `T-5`）`pending_messages` 的三阶段认领与条件写入，并附 `tests/unit/test_repository_statements.py`（AST 静态检查 + 编译产物含 `FOR UPDATE SKIP LOCKED`）— `NFR-1` / `AC-26`
- [x] `T-7` （前置 `T-5`）`messages` 的发信认领与条件 UPDATE（含收割支谓词）— `FR-4` / `AC-29`
- [x] `T-8` （前置 `T-5`）`rejected_messages` 的写入与存在性查询 — `FR-8` / `AC-12`
- [x] `T-9` （前置 `T-5`）`imap_cursors` 的读取、初始化（`UIDNEXT - 1`）、`UIDVALIDITY` 变化时重置、与分拣落库在**同一事务**内推进 — `FR-7` / `AC-39`
- [x] `T-10` （前置 `T-5`）保留期清理：按 `BATCH_SIZE` 分批 + `FOR UPDATE SKIP LOCKED` + `NOT EXISTS` 排除仍有入站待审行指向的出站行 — `NFR-8` / `AC-22`
- [x] `T-11` （前置 `T-5`）`messages` 的出站查询与回复 keyset 分页（`received_at ASC, id ASC`）— `FR-10` / `AC-41`
- [x] `T-12` （前置 `T-5`）入库幂等插入（撞唯一键 / 主键 / `rowcount = 0` 一律按幂等忽略）与两处入站唯一键 — `FR-7` / `AC-11`

## 阶段 3：纯逻辑与外部能力接口

- [x] `T-13` （前置 `T-1`）`core/message_id.py` + `tests/unit/test_message_id.py` — `FR-2` / `AC-4`
- [x] `T-14` （前置 `T-1`）`core/retry.py`（`next_attempt_at(now, attempt)` + 返回码分类）+ `tests/unit/test_retry_policy.py`（含 `AC-21` 的最坏路径算术）— `FR-5` / `AC-9` / `AC-21`
- [x] `T-15` （前置 `T-1`）`core/states.py` 的六态判定 + `tests/unit/test_states.py` — `FR-11` / `AC-36`
- [x] `T-16` （前置 `T-1`）`core/inbound.py` 的 DSN / MDN 结构判定与关联匹配 + `tests/unit/test_inbound_sorting.py` — `FR-9` / `AC-13`
- [x] `T-17` （前置 `T-1`）`providers/review/base.py` 与 `always_pass.py`（显式标注空实现）+ `tests/unit/test_review_placeholder.py` — `NFR-6` / `AC-25`
- [x] `T-18` （前置 `T-17`）`providers/review/factory.py`（依赖覆盖的注入点）— `FR-3` / `AC-6`
- [x] `T-19` （前置 `T-1`）`providers/smtp/base.py` 与 `smtplib_sender.py`：显式写 `From` 与 `Reply-To`、超时上界、只回传返回码与分类 — `FR-2` / `AC-5`
- [x] `T-20` （前置 `T-1`）`providers/imap/base.py`：把邮件解析成中性结构（头字段、MIME 部件、UID 三要素），不做任何判定 — `FR-7` / `AC-39`
- [x] `T-21` （前置 `T-20`）`providers/imap/imapclient_fetcher.py`：`UIDVALIDITY` / `UIDNEXT` 增量、逐封取回、超时 — `FR-7` / `AC-11`

## 阶段 4：五条流与同步路径

- [x] `T-22` （前置 `T-3`、`T-10`）`flows/scheduler.py`（五条任务的启停与调度，间隔取自配置）+ `flows/cleanup.py` 的编排 + `tests/unit/test_scheduler_intervals.py` — `NFR-2` / `AC-23`
- [x] `T-23` （前置 `T-6`、`T-8`、`T-18`）`flows/review_outbound.py`（含收割支）— `FR-3` / `AC-6`
- [x] `T-24` （前置 `T-7`、`T-14`、`T-19`）`flows/send_email.py`（含收割支）— `FR-4` / `AC-7`
- [x] `T-25` （前置 `T-9`、`T-12`、`T-16`、`T-21`）`flows/fetch_inbound.py`：取回 → 判定 → 落库 / 置失败 / 按十一类计数丢弃，游标同事务推进 — `FR-9` / `AC-11`
- [x] `T-26` （前置 `T-6`、`T-8`、`T-18`）`flows/review_inbound.py` — `FR-8` / `AC-12`
- [x] `T-27` （前置 `T-1`）`schemas/messages.py`（请求 / 响应模型、六态取值、时间序列化）+ `tests/unit/test_schemas.py` — `FR-11` / `AC-17`
- [ ] `T-28` （前置 `T-27`、`T-12`）`service/submit_message.py` — `FR-1` / `AC-1`
- [ ] `T-29` （前置 `T-27`、`T-11`）`service/query_message.py` 与 `query_replies.py` — `FR-10` / `AC-15`
- [ ] `T-30` （前置 `T-27`、`T-28`）`api/v1/messages.py` 三个端点 + `tests/unit/test_request_validation.py` — `FR-1` / `AC-2` / `AC-3` / `AC-33`
- [ ] `T-31` （前置 `T-30`、`T-3`）`api/v1/dependencies.py` 的鉴权占位、路由注册、`api/v1/health.py` 补数据库探测 — `F3`（鉴权占位，无对应 `AC`）/ `AC-35`
- [ ] `T-32` （前置 `T-22`、`T-23` ~ `T-26`）`config.validate_startup()` 三条判据 + `main.py` 的 lifespan 装配 + `tests/unit/test_settings_validation.py` + `tests/unit/test_config_hygiene.py` — `NFR-4` / `AC-27` / `AC-32`

## 阶段 5：集成测试、文档与交付

- [ ] `T-33` （前置 `T-5`、`T-22`）`tests/integration/conftest.py`：真实 PostgreSQL 测试库、进程内假 SMTP / 假 IMAP、可替换的假审查实现、清表、容器未起时的跳过标记（§7.2） — `NFR-1` / `AC-29`
- [ ] `T-34` （前置 `T-33`、`T-23`、`T-26`）`tests/integration/test_review_outbound_flow.py` 与 `test_review_inbound_flow.py` — `FR-3` / `FR-8` / `AC-6` / `AC-12`
- [ ] `T-35` （前置 `T-33`、`T-24`）`tests/integration/test_send_flow.py` — `FR-4` / `FR-5` / `AC-4` / `AC-5` / `AC-7` / `AC-8` / `AC-9` / `AC-19` / `AC-21` / `AC-24` / `AC-29` / `AC-30` / `AC-34` / `AC-37` / `AC-40`
- [ ] `T-36` （前置 `T-33`、`T-25`）`tests/integration/test_fetch_inbound_flow.py` — `FR-6` ~ `FR-9` / `AC-10` ~ `AC-14` / `AC-28` / `AC-38` / `AC-39`
- [ ] `T-37` （前置 `T-33`、`T-6`、`T-10`）`tests/integration/test_cleanup_flow.py` 与 `test_repositories.py`（并发抢占、条件 UPDATE、收割支、`NOT EXISTS`、keyset 翻页）— `NFR-8` / `AC-22` / `AC-26` / `AC-29` / `AC-41`
- [ ] `T-38` （前置 `T-33`、`T-31`、`T-32`）`tests/integration/test_api_contract.py` 与 `test_startup_validation.py`、`tests/unit/test_api_contract.py`、`tests/unit/test_constants_consistency.py` — `FR-10` / `NFR-4` / `AC-1` ~ `AC-5` / `AC-15` ~ `AC-18` / `AC-26` / `AC-31` ~ `AC-33` / `AC-35` / `AC-36`
- [ ] `T-39` （前置 `T-38`）七份包占位 docstring 与 `plan.md` §6.2 清单的同步（`migrations/README.md`、`tests/{unit,integration}/README.md`、`providers/review/__init__.py` 等）— 无对应 `AC`（文档与占位）
- [ ] `T-40` （前置 `T-39`）文档同步收尾：`docs/README.md` §3 清单、`docs/configuration.md`、`CHANGELOG.md` — 无对应 `AC`（文档）
- [ ] `T-41` （前置 `T-38`、`T-39`、`T-40`）跑 `bash scripts/check.sh` 五步全绿（`ruff format --check` → `ruff check` → `mypy src` → `pytest`（带覆盖率）→ `mutmut`）— `NFR-4` / `AC-26`
- [ ] `T-42` （前置 `T-41`）逐条按 `AC-n` 回填 `spec.md` §9 的验收结果 — `AC-1` ~ `AC-19`、`AC-21` ~ `AC-41`（`AC-20` 在 `T-45` 回填）；并补 `design.md` §3.4 的**取回结果类型**条目（T-21 已落地承载形态，正式条目欠账在此收口）
- [ ] `T-43` （前置 `T-41`）交付三件脚本：`deploy/migrate.sh`、`deploy/run.sh`、`deploy/check_archive.py` — `FR-14` / `AC-20`
- [ ] `T-44` （前置 `T-43`）`deploy/README.md`、`docs/research/01-delivery-and-deploy.md` 与 `docs/open-questions.md` D2 的就地收窄 — `FR-14` / `AC-20`
- [ ] `T-45` （前置 `T-42`、`T-44`）**交付阶段验证（不入门禁）**：在隔离服务器上完成 `AC-20`——`bash deploy/run.sh` 后 `GET /healthz` 在 60 秒内返回 200，且运行时无任何外部网络依赖；结果回填 `spec.md` §9 — `FR-14` / `NFR-3` / `AC-20`

---

## 完成前自检

- [ ] `tasks.md` 中所有任务已勾选
- [ ] `spec.md` 中每条 `AC-n` 都已被验证，结果已回填「验收结果」
- [ ] 每条 `FR-n` 都有 `AC-n` 覆盖
- [ ] 本地门禁已跑过：`ruff format --check`、`ruff check`、`mypy src`、`pytest`（带覆盖率）、`mutmut`（变异测试）
- [ ] 新增的每条测试都有「能失败」的证据（先红或改坏实现证明它会红），且期望值不是由被测代码生成的（见 `docs/testing.md`）
- [ ] 无密钥、真实主机名 / 端口 / 账号入库
- [ ] 日志与异常信息中不含邮件正文、收件人地址、凭据
- [ ] 涉及不可逆决策的部分已按 `AGENTS.md` §6 更新落点（本特性 `design.md`，或跨特性底座对应的 `docs/research/` 主题册）的决策段
- [ ] **`AC-20` 是唯一不在本地门禁内的条目**：它由 `T-45` 在交付环境验证（本地不满足「与公网隔离」前提），因此它不阻塞本地门禁，但必须在交付后回填结果
- [ ] 该特性的 spec 目录**保留**（不要删除）
