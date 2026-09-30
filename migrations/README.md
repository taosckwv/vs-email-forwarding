# migrations/ — 数据库迁移（Alembic）

本目录存放 **Alembic** 数据库迁移脚本。这是本项目**唯一**允许修改数据库结构的地方。

## 当前状态：尚未初始化

数据表结构**还没有定案**（见 `docs/open-questions.md`），所以本目录下暂时只有这份说明，没有 `env.py`、没有 `versions/`。

**不要提前 `alembic init`，也不要手工添加迁移文件。** 等数据模型规格（`specs/` 下的相关 spec）确认后再生成——因为 Alembic 的 `env.py` 需要绑定 ORM 模型的 `metadata`，而模型本身依赖尚未定案的字段设计。

初始化时机与命令（届时由数据模型规格规定）：

```powershell
uv run alembic init migrations     # 生成 env.py / script.py.mako / versions/
uv run alembic revision --autogenerate -m "建立初始表结构"
uv run alembic upgrade head
```

## 约定

1. **禁止手工 DDL。** 任何建表、加字段、加索引、改约束都必须通过迁移脚本完成，不允许直接在数据库里执行 SQL 了事——否则开发、预演、生产三套环境一定会不一致。
2. **迁移必须可前滚也可回滚。** 每个迁移都要实现 `upgrade()` 与 `downgrade()`。写不出 `downgrade()` 的迁移（例如删列丢了数据）必须在文件里用中文注释写明原因，并在对应的 spec 里记录这条不可逆性。
3. **文件名保留 Alembic 自动生成的 `revision` 哈希前缀**，后面的 `-m` 描述用英文短横线短语（与 `specs/` 的 slug 风格一致），例如 `a1b2c3d4e5f6-create-emails-table.py`。**文件名一律 ASCII，注释用中文。**
4. **改表结构属于不可逆决策。** 按 `AGENTS.md` §6，必须先新增一条 `docs/adr/NNNN-*.md` 说明为什么这样改，再来写迁移。
5. **一个迁移对应一个可解释的意图。** 不要把互不相关的改动塞进同一个迁移文件。
6. **离线和生产环境不手工跑 Alembic**：统一通过 `deploy/migrate.sh`（在应用容器内执行 `uv run alembic upgrade head`）。注意该脚本在迁移存在之前执行会失败，这是预期行为。

## 相关文件

- `docs/adr/` — 表结构相关的决策记录
- `specs/` — 定义表结构的需求与验收标准
- `deploy/migrate.sh` — 部署时执行迁移的入口
- `src/vs_email_forwarding/db/` — ORM 模型与仓储层（`env.py` 的 `target_metadata` 将来自这里）
