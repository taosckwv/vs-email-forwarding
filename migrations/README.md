# migrations/ — 数据库迁移（Alembic）

本目录存放 **Alembic** 数据库迁移脚本。这是本项目**唯一**允许修改数据库结构的地方。

## 当前状态

异步迁移外壳已落地：`alembic.ini`（**不写连接串**）、`env.py`（异步引擎 + 环境变量优先的连接串来源）、`script.py.mako`（迁移脚本模板）。版本文件放在 `versions/`，首版迁移是 `0001_create_mail_tables`（四张表 + 六项索引 + 全部 `CHECK` + 一处自引用外键）。

迁移命令一律**在仓库根目录**执行；连接串由 `env.py` 解析（优先环境变量 `DATABASE_URL`，未设时用 `config.get_settings().database_url`）：

```bash
uv run alembic -c migrations/alembic.ini history                     # 查看版本链
uv run alembic -c migrations/alembic.ini upgrade head                # 前滚到最新
uv run alembic -c migrations/alembic.ini downgrade -1                # 回滚一个版本
uv run alembic -c migrations/alembic.ini revision --rev-id 0002 -m "add something"
```

## 约定

1. **禁止手工 DDL。** 任何建表、加字段、加索引、改约束都必须通过迁移脚本完成，不允许直接在数据库里执行 SQL 了事——否则开发、预演、生产三套环境一定会不一致。
2. **迁移必须可前滚也可回滚。** 每个迁移都要实现 `upgrade()` 与 `downgrade()`。写不出 `downgrade()` 的迁移（例如删列丢了数据）必须在文件里用中文注释写明原因，并在对应的 spec 里记录这条不可逆性。
3. **文件名用「四位序号 + 下划线 + 英文短语」**，序号与 `revision` 一致，例如 `0001_create_mail_tables.py`。序号用 `--rev-id` 指定（`0001`、`0002`…，`file_template` 已按此配置），`down_revision` 指向上一个序号，首个迁移为 `None`。**文件名一律 ASCII，注释用中文。**
4. **改表结构属于不可逆决策。** 按 `AGENTS.md` §6，必须先把决策写进它的落点（本特性的 `specs/<特性>/design.md`；跨特性底座写 `docs/research/` 下对应主题册）的『结论』段（被否决的方向写进『已否决方向』段），再来写迁移。
5. **一个迁移对应一个可解释的意图。** 不要把互不相关的改动塞进同一个迁移文件。
6. **离线和生产环境不手工跑 Alembic**：统一通过 `deploy/migrate.sh`（在应用容器内执行 `uv run alembic upgrade head`）。注意该脚本在迁移存在之前执行会失败，这是预期行为。
7. **不在本目录写死连接串。** `alembic.ini` 里没有 `sqlalchemy.url` 取值，`env.py` 只从环境变量 `DATABASE_URL` 或 `config.get_settings().database_url` 取；需要覆盖连接串时用环境变量，不要改仓库文件。`--sql` 离线模式不支持（见 `env.py` 的模块说明）。

## 相关文件

- `docs/research/` — 跨特性底座的当前结论与决策记录；本特性的数据模型见 `specs/0001-email-forwarding-service/design.md`
- `specs/` — 定义需求与验收标准（`spec.md`）；设计结论在对应特性的 `design.md`
- `deploy/migrate.sh` — 部署时执行迁移的**唯一**入口（约定第 6 条）
- `src/vs_email_forwarding/db/` — ORM 模型与仓储层；`env.py` 的 `target_metadata` 取 `db/models.py` 的 `metadata`
