# migrations/ — 数据库迁移（Alembic）

本目录存放 **Alembic** 数据库迁移脚本。这是本项目**唯一**允许修改数据库结构的地方。

## 当前状态：尚未初始化

数据表结构**已定**（见 `specs/0001-email-forwarding-service/design.md` 的「数据模型」一节），但本目录尚未初始化，所以暂时只有这份说明，没有 `env.py`、没有 `versions/`。

**不要提前 `alembic init`，也不要手工添加迁移文件。** 初始化与首次迁移按该特性的 `plan.md` / `tasks.md` 的顺序落地——因为 Alembic 的 `env.py` 需要绑定 ORM 模型的 `metadata`，而模型本身依赖 `design.md` 里已定的表与字段清单。

初始化时机与命令（届时由数据模型规格规定）：

```bash
uv run alembic init migrations     # 生成 env.py / script.py.mako / versions/
uv run alembic revision --autogenerate -m "建立初始表结构"
uv run alembic upgrade head
```

## 约定

1. **禁止手工 DDL。** 任何建表、加字段、加索引、改约束都必须通过迁移脚本完成，不允许直接在数据库里执行 SQL 了事——否则开发、预演、生产三套环境一定会不一致。
2. **迁移必须可前滚也可回滚。** 每个迁移都要实现 `upgrade()` 与 `downgrade()`。写不出 `downgrade()` 的迁移（例如删列丢了数据）必须在文件里用中文注释写明原因，并在对应的 spec 里记录这条不可逆性。
3. **文件名保留 Alembic 自动生成的 `revision` 哈希前缀**，后面的 `-m` 描述用英文短横线短语（与 `specs/` 的 slug 风格一致），例如 `a1b2c3d4e5f6-create-emails-table.py`。**文件名一律 ASCII，注释用中文。**
4. **改表结构属于不可逆决策。** 按 `AGENTS.md` §6，必须先把决策写进它的落点（本特性的 `specs/<特性>/design.md`；跨特性底座写 `docs/research/` 下对应主题册）的『结论』段（被否决的方向写进『已否决方向』段），再来写迁移。
5. **一个迁移对应一个可解释的意图。** 不要把互不相关的改动塞进同一个迁移文件。
6. **离线和生产环境不手工跑 Alembic**：统一通过 `deploy/migrate.sh`（在应用容器内执行 `uv run alembic upgrade head`）。注意该脚本在迁移存在之前执行会失败，这是预期行为。

## 相关文件

- `docs/research/` — 跨特性底座的当前结论与决策记录；本特性的数据模型见 `specs/0001-email-forwarding-service/design.md`
- `specs/` — 定义需求与验收标准（`spec.md`）；设计结论在对应特性的 `design.md`
- `deploy/migrate.sh` — 部署时执行迁移的入口
- `src/vs_email_forwarding/db/` — ORM 模型与仓储层（`env.py` 的 `target_metadata` 将来自这里）
