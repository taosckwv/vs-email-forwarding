# scripts/ —— 本地开发与门禁脚本

本目录只放**本地开发与门禁**脚本。部署相关脚本（构建镜像、离线交付、迁移、启动）
全部在 `deploy/`，**不要在这里另起一套**（见 `AGENTS.md` §3、§4）。

所有脚本都从**任意工作目录**调用都能正确工作：bash 版用 `${BASH_SOURCE[0]}` 推导仓库根，然后切到仓库根再执行。

仓库**只保留 bash 脚本**：开发机是 Linux，Windows 版的 `check.ps1` / `dev-up.ps1` 已删除（见 `docs/research/05-dev-and-build-environment.md`）。

---

## 脚本清单

| 脚本 | 用途 | 参数 | 前置条件 |
|---|---|---|---|
| `check.sh` | **一键门禁**：依次跑 `ruff format --check` → `ruff check` → `mypy src` → `pytest` | 无（刻意不接受参数，保证所有人跑的是同一套门禁） | 已安装 **uv**（`uv --version` 可用）。**不需要 Docker**（纯逻辑单测不依赖容器） |
| `dev-up.sh` | 起开发依赖：PostgreSQL 17 + Mailpit（`compose.dev.yaml`），并打印连接信息 | 无 | 已安装并启动 **Docker**（Linux 上的 Docker 不需要硬件虚拟化） |

---

## 一键门禁：为什么它这么重要

**本项目不跑 CI**（见 `AGENTS.md` §9 与 `docs/research/03-dependencies-and-toolchain.md`）。没有任何自动兜底：
不会有人在 PR 上替你跑检查，配置错误也不会被流水线拦住。

所以：

> **提交前必须自己跑 `bash scripts/check.sh`，必须全绿。**

仓库只保留 bash 脚本，执行的步骤、顺序、失败行为一致：

1. `uv run ruff format --check .` —— 代码格式检查
2. `uv run ruff check .` —— 静态检查（lint）
3. `uv run mypy src` —— 类型检查
4. `uv run pytest` —— 单元与集成测试

任何一步失败立即以**非零退出码**结束，后续步骤不再执行；输出中有中文步骤提示
（`[1/4] 代码格式检查` 这类），失败时明确指出是哪一步。

### 关于 docker 与门禁的关系

- 上面 4 步本身**不需要 Docker**：`tests/unit/` 是纯逻辑测试。
- `tests/integration/` 需要真实 PostgreSQL 与邮件服务，由 `scripts/dev-up.sh` 在本机起容器提供——**开发机已装 Docker，本机跑得了**（见 `docs/research/05-dev-and-build-environment.md`）；未起容器时按 pytest 的标记跳过（标记约定见 `docs/testing.md`，随测试规格一起落地）。
- 门禁脚本刻意不加"先起容器"的逻辑：让门禁的依赖尽量少，才能做到"随手就能跑"。

---

## 开发依赖：`dev-up` 起的是什么

**这套开发依赖在开发机（CentOS 8 Linux）上起得来**——开发机已装 Docker，用 `bash scripts/dev-up.sh` 起（见 `docs/research/05-dev-and-build-environment.md`）。

`compose.dev.yaml` 里只有两个服务：

| 服务 | 端口 | 作用 |
|---|---|---|
| `postgres:17` | `127.0.0.1:5432` | 开发库 `vs_email_forwarding_dev`（测试库 `vs_email_forwarding_test` 需手工创建，命令见 `compose.dev.yaml` 末尾） |
| `axllent/mailpit` | `1025`(SMTP) / `1143`(IMAP) / `8025`(Web UI) | 验证**完整回路**：发出去 → 被拉回来 → 关联上原邮件 |

Mailpit 的 Web UI：<http://127.0.0.1:8025>。

应用本体**不在容器里跑**，用 uv 在宿主机跑（热重载、调试都方便）：

```bash
uv sync
uv run uvicorn vs_email_forwarding.main:app --reload
```

停止依赖：

```bash
docker compose -f compose.dev.yaml down          # 保留数据
docker compose -f compose.dev.yaml down -v       # 连命名卷一起删（测试库/开发库全清）
```

---

## 常见问题

| 现象 | 处理 |
|---|---|
| `check.sh` 报「找不到 uv 命令」 | 先安装 uv（安装方式见 uv 官方文档），确认新开的终端里 `uv --version` 可用 |
| `dev-up.sh` 报「找不到 docker」 | 本机没装 Docker；按 `docs/research/05-dev-and-build-environment.md` 装（Linux 上的 Docker 不需要硬件虚拟化） |
| `dev-up.*` 报「连不上 Docker 守护进程」 | 两种原因：守护进程没启动（`systemctl start docker`），或**当前用户不在 `docker` 组**——socket 是 `root:docker 660`，且重新登录后组权限才生效 |
| `dev-up.sh` 报「permission denied ... docker.sock」 | 同上：把用户加进 `docker` 组（`usermod -aG docker <用户>`）后**重新登录** |
| 端口 5432 / 1025 / 1143 / 8025 被占用 | 先 `docker compose -f compose.dev.yaml down`；仍冲突则改 `compose.dev.yaml` 里的宿主端口映射 |
| `bash scripts/*.sh` 报 `$'\r': command not found` | 脚本被转成了 CRLF。`sed -i 's/\r$//' scripts/*.sh`，并检查 git 的 `core.autocrlf` 设置（这批脚本应以 LF 提交） |
| 脚本在仓库子目录里执行报路径错 | 不该发生（脚本自己推导仓库根）；若发生请当成 bug 反馈，不要手工 `cd` 绕过 |
