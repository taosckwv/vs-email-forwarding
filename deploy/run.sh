#!/usr/bin/env bash
# =============================================================================
# 在**离线服务器**上启动 vs-email-forwarding，并对 /healthz 做健康检查轮询。
#
# 用法：
#     bash deploy/run.sh
#     COMPOSE_FILE=/opt/vs-email-forwarding/compose.yaml bash deploy/run.sh
#
# 做的事：
#     1) docker compose up -d（应用 + 同机 PostgreSQL）
#     2) 对 http://127.0.0.1:${HTTP_PORT}/healthz 轮询，直到 200 或超过重试上限
#     3) 打印容器状态与查看日志的命令
#     4) 失败时给出排查提示（不是"启动命令返回 0 就算成功"）
#
# 为什么一定要轮询：`up -d` 返回 0 只代表容器被创建/启动了，
# 应用真正可用（lifespan 起来、四条轮询流开始跑）还要几秒；
# 数据库首次 initdb 更慢。脚本必须等到 /healthz 真的通过才算启动成功。
#
# 前置条件：已 load 镜像（deploy/load-image.sh）、.env 已就位、数据目录属主已修好。
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$(cd -- "${SCRIPT_DIR}/.." && pwd)/compose.yaml}"

# 健康检查重试参数（可用环境变量覆盖）
HEALTH_RETRIES="${HEALTH_RETRIES:-30}"
HEALTH_INTERVAL="${HEALTH_INTERVAL:-2}"

info() { echo "[信息] $*"; }
fail() { echo "[失败] $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail "找不到 docker 命令。"
[ -f "${COMPOSE_FILE}" ] || fail "找不到 compose 文件：${COMPOSE_FILE}"

COMPOSE_DIR="$(cd -- "$(dirname -- "${COMPOSE_FILE}")" && pwd)"
cd -- "${COMPOSE_DIR}"

# --- 读取对外端口：优先 .env 里的 HTTP_PORT，读不到用 compose.yaml 的默认 8000 ----
HTTP_PORT=""
if [ -f .env ]; then
    # 取最后一个 HTTP_PORT= 赋值，只保留数字：这样顺带容忍 Windows 编辑留下的 CR、
    # 值两边的引号/空格，以及行尾注释（"8000  # 对外端口"）。
    HTTP_PORT="$(grep -E '^[[:space:]]*HTTP_PORT[[:space:]]*=' .env | tail -n 1 | cut -d= -f2- | tr -cd '0-9' || true)"
fi
if [ -z "${HTTP_PORT}" ]; then
    HTTP_PORT="8000"
    info "未从 .env 读到 HTTP_PORT，按默认 8000 做健康检查。"
fi
HEALTH_URL="http://127.0.0.1:${HTTP_PORT}/healthz"

# --- 1. 启动 -------------------------------------------------------------------
info "compose 文件：${COMPOSE_FILE}"
info "执行：docker compose up -d"
docker compose -f "${COMPOSE_FILE}" up -d

# --- 2. 健康检查轮询 -----------------------------------------------------------
echo
info "等待 ${HEALTH_URL} 就绪（最多 $((HEALTH_RETRIES * HEALTH_INTERVAL)) 秒）"

if ! command -v curl >/dev/null 2>&1; then
    info "服务器上没有 curl，跳过自动健康检查。请人工验证："
    info "  docker compose -f ${COMPOSE_FILE} exec app python -c \"import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').status)\""
    HEALTH_OK=1
    HEALTH_SKIPPED=1
else
    HEALTH_OK=0
    HEALTH_SKIPPED=0
    for i in $(seq 1 "${HEALTH_RETRIES}"); do
        if curl -fsS --max-time 3 "${HEALTH_URL}" >/dev/null 2>&1; then
            HEALTH_OK=1
            info "第 ${i} 次探测成功：/healthz 返回 200"
            break
        fi
        printf '[信息] 第 %s/%s 次探测未通过，%s 秒后重试\n' "${i}" "${HEALTH_RETRIES}" "${HEALTH_INTERVAL}"
        sleep "${HEALTH_INTERVAL}"
    done
fi

# --- 3. 容器状态 ---------------------------------------------------------------
echo
info "容器状态："
docker compose -f "${COMPOSE_FILE}" ps

if [ "${HEALTH_OK}" -ne 1 ]; then
    cat >&2 <<EOF

[失败] 健康检查未在预期时间内通过（${HEALTH_URL}）。
排查顺序：
  1) 看应用日志（**优先看这个**）：
       docker compose -f ${COMPOSE_FILE} logs --tail=200 app
  2) 看数据库日志与健康状态：
       docker compose -f ${COMPOSE_FILE} logs --tail=100 postgres
       docker compose -f ${COMPOSE_FILE} ps
  3) 常见原因：
       - .env 里数据库连接参数填错（应用连不上库，lifespan 起不来）
       - 数据目录属主不对：chown -R 999:999 ./data/postgres
       - 端口被占用：ss -lntp | grep ${HTTP_PORT}，或改 .env 里的 HTTP_PORT
       - 防火墙未放行：firewall-cmd --list-ports（放行命令见 deploy/README.md）
       - 镜像架构不匹配：docker image inspect --format '{{.Os}}/{{.Architecture}}' \${VS_EMAIL_FORWARDING_IMAGE}
         （必须是 linux/amd64；架构错了会 exec format error）
  4) 回滚：把 .env 里的 VS_EMAIL_FORWARDING_IMAGE 改回上一个版本 tag，再执行本脚本。
EOF
    exit 1
fi

# --- 4. 成功后的提示 -----------------------------------------------------------
if [ "${HEALTH_SKIPPED}" -eq 1 ]; then
    echo
    echo "服务已启动（健康检查因缺少 curl 被跳过，请按上面的命令人工确认 /healthz）。"
else
    cat <<EOF

服务已启动并通过健康检查。

  健康检查 : ${HEALTH_URL}
EOF
fi

cat <<EOF
  查看应用日志 : docker compose -f ${COMPOSE_FILE} logs -f app
  查看全部日志 : docker compose -f ${COMPOSE_FILE} logs -f
  停止服务     : docker compose -f ${COMPOSE_FILE} down
  重启应用     : docker compose -f ${COMPOSE_FILE} restart app

提醒：日志里不应出现邮件正文、收件人地址或凭据（见 AGENTS.md §8）。
EOF
