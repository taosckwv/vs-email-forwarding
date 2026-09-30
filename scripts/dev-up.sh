#!/usr/bin/env bash
# =============================================================================
# vs-email-forwarding 开发依赖一键启动（Linux / macOS 版，CentOS 8 可用）
#
# 用 compose.dev.yaml 起开发用基础设施：PostgreSQL 17 + Mailpit（SMTP/IMAP/Web UI）。
# 应用本体不在容器里跑，用 uv 在宿主机跑：
#     uv run uvicorn vs_email_forwarding.main:app --reload
#
# 用法（可以从任意目录调用，脚本自己推导仓库根）：
#     bash scripts/dev-up.sh
#
# 前置条件：**已安装 Docker**（Windows 上是 Docker Desktop，见
#   docs/open-questions.md D3）。没装的话本脚本只打印安装提示并退出。
# =============================================================================
set -euo pipefail

# 仓库根 = 本脚本所在目录（scripts/）的上一级
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# --- 前置检查：Docker ---------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
    echo "[阻塞] 找不到 docker 命令。" >&2
    echo "请先安装 Docker（Windows 上是 Docker Desktop，需要 WSL2，可能需要管理员权限并重启）：" >&2
    echo "  https://docs.docker.com/desktop/setup/install/windows-install/" >&2
    echo "装好后启动 Docker，确认 \`docker info\` 正常，再重跑本脚本。" >&2
    exit 127
fi

if ! docker info >/dev/null 2>&1; then
    echo "[阻塞] docker 命令存在，但连不上 Docker 守护进程。" >&2
    echo "请先启动 Docker Desktop / dockerd，再重跑本脚本。" >&2
    exit 1
fi

# --- 起依赖 -------------------------------------------------------------------
echo "==> 启动开发依赖（PostgreSQL + Mailpit）"
docker compose -f compose.dev.yaml up -d

# --- 打印连接信息（与 compose.dev.yaml 保持一致） ------------------------------
cat <<'EOF'

开发依赖已启动：
  Mailpit Web UI : http://127.0.0.1:8025   （看所有收下的邮件，也可手动回信）
  SMTP           : 127.0.0.1:1025          （应用发信走这里）
  IMAP           : 127.0.0.1:1143          （应用拉取入站回复走这里）
  PostgreSQL     : 127.0.0.1:5432
    开发库       : vs_email_forwarding_dev  (用户 vs_email_forwarding / 口令 vs_email_forwarding_dev)
    测试库       : vs_email_forwarding_test （需手工创建，命令见 compose.dev.yaml 末尾说明）

Mailpit 的用途：验证「发出去 -> 被拉回来 -> 关联上原邮件」的完整回路，
不需要真实邮箱账号，也不会把测试邮件发到外网。

接下来（应用本体在宿主机跑，不在容器里）：
  uv sync
  uv run uvicorn vs_email_forwarding.main:app --reload

停止依赖：docker compose -f compose.dev.yaml down
连数据一起清掉：docker compose -f compose.dev.yaml down -v
EOF
