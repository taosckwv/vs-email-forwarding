#!/usr/bin/env bash
# =============================================================================
# 在**离线服务器**上执行数据库迁移（Alembic）。
#
# 用法（从仓库根或任意目录都可，脚本自己定位 compose.yaml）：
#     bash deploy/migrate.sh
#     COMPOSE_FILE=/opt/mail-relay/compose.yaml bash deploy/migrate.sh
#
# 做的事：在应用镜像里跑一次性容器执行 `uv run alembic upgrade head`。
#   * 用 `docker compose run --rm` 而不是 exec：不依赖应用容器正在运行，
#     也不会把迁移混进服务启动流程（迁移失败时服务不会被"半启动"）。
#   * 不带 --no-deps：compose 会先按依赖关系把 postgres 拉起并等它 healthy，
#     避免"库还没就绪就迁移"。
#   * 应用镜像里带着 uv（见 Dockerfile 的说明），所以离线服务器不需要装 Python/uv。
#
# ⚠️ 预期失败（重要，不要误判为部署故障）：
#     Alembic **尚未初始化** —— `migrations/` 目录与 alembic.ini 要等数据模型规格
#     落地后才会生成（见 docs/open-questions.md 的 A1–A5 与 AGENTS.md §5）。
#     在那之前，本脚本一定报找不到 alembic / 找不到配置，这是**预期行为**。
#     判断方法：如果报错是 "No such file or directory: alembic.ini"、
#     "alembic: command not found" 或 "migrations/ ... not found"，
#     说明规格还没落地；如果报错是连接数据库失败，那才是部署问题。
#
# 前置条件：已执行 deploy/load-image.sh 加载镜像；.env 已就位（含数据库连接参数）。
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# 默认用交付目录（deploy/ 的上一级）里的 compose.yaml
COMPOSE_FILE="${COMPOSE_FILE:-$(cd -- "${SCRIPT_DIR}/.." && pwd)/compose.yaml}"

info() { echo "[信息] $*"; }
fail() { echo "[失败] $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail "找不到 docker 命令。"
[ -f "${COMPOSE_FILE}" ] || fail "找不到 compose 文件：${COMPOSE_FILE}"

# 切到 compose 文件所在目录执行：compose 的 .env 插值、相对路径挂载都按项目目录解析
COMPOSE_DIR="$(cd -- "$(dirname -- "${COMPOSE_FILE}")" && pwd)"
cd -- "${COMPOSE_DIR}"

info "compose 文件：${COMPOSE_FILE}"
info "执行：docker compose run --rm app uv run alembic upgrade head"

if ! docker compose -f "${COMPOSE_FILE}" run --rm app uv run alembic upgrade head; then
    cat >&2 <<'EOF'

[失败] 迁移未成功。
  若错误是「找不到 alembic.ini / migrations / alembic 命令」：
      这是**预期行为** —— Alembic 尚未初始化，等数据模型规格落地后才有 migrations/。
      此时可以先跑 `bash deploy/run.sh` 把服务起起来验证镜像与数据库连通性。
  若错误是连接数据库失败：
      检查 .env 里的数据库连接参数与 MAIL_RELAY_IMAGE；并确认 postgres 容器 healthy：
      docker compose -f compose.yaml ps
EOF
    exit 1
fi

info "迁移完成。下一步：bash deploy/run.sh"
