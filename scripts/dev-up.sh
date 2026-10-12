#!/usr/bin/env bash
# =============================================================================
# vs-email-forwarding 开发依赖一键启动（Linux / macOS 版，CentOS 8 可用）
#
# 用 compose.dev.yaml 起开发用基础设施：PostgreSQL 17 + GreenMail（SMTP/IMAP/POP3）。
# 应用本体不在容器里跑，用 uv 在宿主机跑：
#     uv run uvicorn vs_email_forwarding.main:app --reload
#
# 用法（可以从任意目录调用，脚本自己推导仓库根）：
#     bash scripts/dev-up.sh
#
# 前置条件：宿主机能跑 Docker（开发机已装，见 docs/research/05-dev-and-build-environment.md）。
# =============================================================================
set -euo pipefail

# 仓库根 = 本脚本所在目录（scripts/）的上一级
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# --- 前置检查：Docker ---------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
    echo "[阻塞] 找不到 docker 命令。" >&2
    echo "请先在这台机器上安装 Docker（Linux 上的 Docker 不需要硬件虚拟化）：" >&2
    echo "  https://docs.docker.com/engine/install/" >&2
    echo "装好后启动 docker，确认 \`docker info\` 正常，再重跑本脚本。" >&2
    exit 127
fi

if ! docker info >/dev/null 2>&1; then
    echo "[阻塞] docker 命令存在，但连不上 Docker 守护进程。" >&2
    echo "请先启动 docker 守护进程（dockerd），再重跑本脚本。" >&2
    exit 1
fi

# --- 起依赖 -------------------------------------------------------------------
echo "==> 启动开发依赖（PostgreSQL + GreenMail）"
docker compose -f compose.dev.yaml up -d

# --- 打印连接信息（与 compose.dev.yaml 保持一致） ------------------------------
cat <<'EOF'

开发依赖已启动：
  SMTP (GreenMail): 127.0.0.1:3025          （应用发信走这里，也用来模拟"同事的回复"）
  IMAP (GreenMail): 127.0.0.1:3143          （应用拉取入站回复走这里）
  POP3 (GreenMail): 127.0.0.1:3110          （想在邮件客户端里看收下的信时用）
  PostgreSQL     : 127.0.0.1:5432
    开发库       : vs_email_forwarding_dev  (用户 vs_email_forwarding / 口令 vs_email_forwarding_dev)
    测试库       : vs_email_forwarding_test （需手工创建，命令见 compose.dev.yaml 末尾说明）

GreenMail 的用途：验证「发出去 -> 被拉回来 -> 关联上原邮件」的完整回路，
不需要真实邮箱账号，也不会把测试邮件发到外网。
注意两点：① 它**按账号分信箱**——投给 reply@example.com 的信要用该账号登录 IMAP 才看得到；
② 它启动时自带 auth.disabled，账号口令随便填（但要与 IMAP 侧一致）。
本机 .env 的本地联调取值见 .env.example 里 SMTP/IMAP 段末尾的说明。

接下来（应用本体在宿主机跑，不在容器里）：
  uv sync
  uv run uvicorn vs_email_forwarding.main:app --reload

停止依赖：docker compose -f compose.dev.yaml down
连数据一起清掉：docker compose -f compose.dev.yaml down -v
EOF
