# scripts/ —— 本地开发与门禁脚本

本目录只放**本地开发与门禁**脚本。部署相关脚本（构建镜像、离线交付、迁移、启动）
全部在 `deploy/`，**不要在这里另起一套**（见 `AGENTS.md` §3、§4）。

所有脚本都从**任意工作目录**调用都能正确工作：PowerShell 版用 `$PSScriptRoot`、
bash 版用 `${BASH_SOURCE[0]}` 推导仓库根，然后切到仓库根再执行。

---

## 脚本清单

| 脚本 | 用途 | 参数 | 前置条件 |
|---|---|---|---|
| `check.ps1` | **一键门禁（Windows）**：依次跑 `ruff format --check` → `ruff check` → `mypy src` → `pytest` | 无（刻意不接受参数，保证所有人跑的是同一套门禁） | 已安装 **uv**（`uv --version` 可用）。**不需要 Docker**（纯逻辑单测不依赖容器） |
| `check.sh` | **一键门禁（Linux/macOS/CentOS 8）**，与 `check.ps1` **行为完全一致** | 无 | 已安装 **uv**。**不需要 Docker** |
| `dev-up.ps1` | 起开发依赖：PostgreSQL 17 + Mailpit（`compose.dev.yaml`），并打印连接信息 | 无 | 已安装并启动 **Docker Desktop**（见 `docs/open-questions.md` D3） |
| `dev-up.sh` | 同上（Linux/macOS） | 无 | 已安装并启动 **Docker** |

---

## 一键门禁：为什么它这么重要

**本项目不跑 CI**（见 `AGENTS.md` §9 与 ADR-0010）。没有任何自动兜底：
不会有人在 PR 上替你跑检查，配置错误也不会被流水线拦住。

所以：

> **提交前必须自己跑 `scripts/check.ps1`（Windows）或 `bash scripts/check.sh`（Linux），
> 必须全绿。**

两个版本执行的步骤、顺序、失败行为**完全一致**：

1. `uv run ruff format --check .` —— 代码格式检查
2. `uv run ruff check .` —— 静态检查（lint）
3. `uv run mypy src` —— 类型检查
4. `uv run pytest` —— 单元与集成测试

任何一步失败立即以**非零退出码**结束，后续步骤不再执行；输出中有中文步骤提示
（`[1/4] 代码格式检查` 这类），失败时明确指出是哪一步。

### 关于 docker 与门禁的关系

- 上面 4 步本身**不需要 Docker**：`tests/unit/` 是纯逻辑测试。
- 但 `tests/integration/` 需要真实 PostgreSQL 与邮件服务；这些用例要么在**开发机**上
  对着 `scripts/dev-up.*` 起的容器跑，要么按 pytest 的标记跳过（标记约定见
  `docs/testing.md`，随测试规格一起落地）。
- 门禁脚本刻意不加"先起容器"的逻辑：让门禁的依赖尽量少，才能做到"随手就能跑"。

---

## 开发依赖：`dev-up` 起的是什么

`compose.dev.yaml` 里只有两个服务：

| 服务 | 端口 | 作用 |
|---|---|---|
| `postgres:17` | `127.0.0.1:5432` | 开发库 `mail_relay_dev`（测试库 `mail_relay_test` 需手工创建，命令见 `compose.dev.yaml` 末尾） |
| `axllent/mailpit` | `1025`(SMTP) / `1143`(IMAP) / `8025`(Web UI) | 验证**完整回路**：发出去 → 被拉回来 → 关联上原邮件 |

Mailpit 的 Web UI：<http://127.0.0.1:8025>。

应用本体**不在容器里跑**，用 uv 在宿主机跑（热重载、调试都方便）：

```powershell
uv sync
uv run uvicorn mail_relay.main:app --reload
```

```bash
uv sync
uv run uvicorn mail_relay.main:app --reload
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
| `check.ps1` 报「找不到 uv 命令」 | 先安装 uv（安装方式见 uv 官方文档），确认新开的终端里 `uv --version` 可用 |
| `dev-up.*` 报「找不到 docker」 | 安装并启动 Docker Desktop（Windows 需要 WSL2，安装可能需要管理员权限并重启） |
| `dev-up.*` 报「连不上 Docker 守护进程」 | Docker Desktop 没启动；等托盘图标变为运行中再重试 |
| 端口 5432 / 1025 / 1143 / 8025 被占用 | 先 `docker compose -f compose.dev.yaml down`；仍冲突则改 `compose.dev.yaml` 里的宿主端口映射 |
| `bash scripts/*.sh` 报 `$'\r': command not found` | 脚本被转成了 CRLF。`sed -i 's/\r$//' scripts/*.sh`，并检查 git 的 `core.autocrlf` 设置（这批脚本应以 LF 提交） |
| `.ps1` 里的中文变成乱码，甚至报出奇怪的语法错误 | 这几个 `.ps1` 必须**以「UTF-8 with BOM」保存**：Windows PowerShell 5.1 只会按系统 ANSI 代码页读取无 BOM 的文件，中文注释会被解码坏并破坏语法（已实测）。编辑后确认 BOM 还在（VS Code 右下角编码选 `UTF-8 with BOM`）。`scripts/*.sh`、YAML 与文档则相反：**必须无 BOM、LF**。 |
| 脚本在仓库子目录里执行报路径错 | 不该发生（脚本自己推导仓库根）；若发生请当成 bug 反馈，不要手工 `cd` 绕过 |
