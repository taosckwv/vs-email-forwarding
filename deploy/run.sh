#!/usr/bin/env bash
# =============================================================================
# vs-email-forwarding 服务器侧启动脚本：起服务 + 对 /healthz 做健康检查轮询。
#
# 做的事（顺序固定：先迁移、再起服务，见 docs/research/01-delivery-and-deploy.md
# 的「维护与回滚」段，以及 specs/0001-email-forwarding-service/plan.md §10.1 的
# 「回滚入口」行——迁移入口固定为 deploy/migrate.sh）：
#     1) 前置检查：docker 可用、compose v2 可用、compose 文件与 .env 就位；
#     2) 确认应用镜像已在**本机**（离线服务器上 compose 不能去远端拉），
#        并复检它的架构必须是 linux/amd64；
#     3) docker compose up -d（应用 + 同机 PostgreSQL）；
#     4) 轮询 http://127.0.0.1:${HTTP_PORT}/healthz，直到 200 或超过重试上限；
#     5) 打印容器状态与后续命令（含清场与「出站连接为零」的观察法）。
#
# 为什么一定要轮询（判据真源：docs/research/01-delivery-and-deploy.md
# 的「待实现脚本必须保留的方法性结论」段）：`docker compose up -d` 返回 0
# **不等于**服务可用——它只代表容器被创建/启动了。应用真正可用（lifespan 起来、
# 五条后台流开始跑）还要几秒，数据库首次 initdb 更慢。所以「up -d + /healthz 轮询」
# 两件事都做完才算启动成功。
#
# 用法（可以从任意目录调用；脚本自己推导默认的 compose.yaml）：
#     bash deploy/run.sh
#     HTTP_PORT=8080 bash deploy/run.sh
#     COMPOSE_FILE=/opt/vs-email-forwarding/compose.yaml bash deploy/run.sh
#     bash deploy/run.sh --skip-migrate      # 前提：迁移已经跑过
#     bash deploy/run.sh --help
#
# 环境变量（都有默认值）：
#     COMPOSE_FILE      compose 文件路径（默认：本脚本上一级的 compose.yaml）
#     HTTP_PORT         对外端口（默认：从 .env 的 HTTP_PORT 读，读不到用 8000）
#     HEALTH_RETRIES    健康检查重试次数（默认 30）
#     HEALTH_INTERVAL   每次重试之间的间隔秒数（默认 2）
#     SKIP_MIGRATE      置 1 等同 --skip-migrate
#
# 前置条件：镜像已 load 到本机、.env 已就位、数据目录属主已修好
#          （首次部署：mkdir -p ./data/postgres && chown -R 999:999 ./data/postgres，
#           并按宿主 SELinux 为 Enforcing 的事实给挂载目录打标签）。
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_COMPOSE_FILE="$(cd -- "${SCRIPT_DIR}/.." && pwd)/compose.yaml"
COMPOSE_FILE="${COMPOSE_FILE:-${DEFAULT_COMPOSE_FILE}}"

# 健康检查重试参数（可用环境变量覆盖；默认 30 次 × 2s）。
HEALTH_RETRIES="${HEALTH_RETRIES:-30}"
HEALTH_INTERVAL="${HEALTH_INTERVAL:-2}"

# 目标架构：compose.yaml 里 `platform: linux/amd64` 只是**运行期保险**，
# 真正决定架构的是构建期 `docker build --platform linux/amd64`；这里做启动前**复检**。
TARGET_PLATFORM="linux/amd64"

# compose.yaml 里应用镜像的默认值（与该文件的 `${VS_EMAIL_FORWARDING_IMAGE:-...}` 一致）。
DEFAULT_IMAGE_REF="vs-email-forwarding:latest"

# 从 .env 解析 HTTP_PORT 读不到时用的兜底值（与 compose.yaml 的 `${HTTP_PORT:-8000}` 一致）。
DEFAULT_HTTP_PORT="8000"

SKIP_MIGRATE="${SKIP_MIGRATE:-0}"

info() { echo "[信息] $*"; }
warn() { echo "[警告] $*" >&2; }
fail() { echo "[失败] $*" >&2; exit 1; }

usage() {
    cat <<'EOF'
用法：bash deploy/run.sh [选项]

启动服务（docker compose up -d）并轮询 /healthz，直到返回 200 或超过重试上限。
默认先跑一次迁移（bash deploy/migrate.sh），因为部署顺序固定为「先迁移、再起服务」。

选项：
  -h, --help        显示本说明
      --skip-migrate  跳过迁移步骤（前提：迁移已经跑过）
      --compose-file F  指定 compose 文件（默认：本脚本上一级的 compose.yaml；
                        也可以用环境变量 COMPOSE_FILE 指定）

环境变量：
  COMPOSE_FILE     同 --compose-file
  HTTP_PORT        对外端口（覆盖 .env 里的 HTTP_PORT；都不给则按 8000）
  HEALTH_RETRIES   健康检查重试次数（默认 30）
  HEALTH_INTERVAL  重试间隔秒数（默认 2）
  SKIP_MIGRATE     置 1 等同 --skip-migrate

退出码：
  0 = 服务已起来且 /healthz 返回 200
  1 = 前置条件不满足，或健康检查未在预期时间内通过（启动失败）
EOF
}

# 解析参数：只认这四条，其余一律拒绝（避免运维以为传了参数、其实被静默忽略）。
while [ "$#" -gt 0 ]; do
    case "$1" in
        -h | --help)
            usage
            exit 0
            ;;
        --skip-migrate)
            SKIP_MIGRATE=1
            shift
            ;;
        --compose-file)
            [ "$#" -ge 2 ] || fail "--compose-file 需要一个参数（compose 文件路径）。"
            COMPOSE_FILE="$2"
            shift 2
            ;;
        *)
            fail "不认识的参数：$1（用 --help 看用法）。"
            ;;
    esac
done

# 从 .env 读一个键的值：容忍 CR、值两边的引号/空格，以及行尾注释。
# 注释切法按**成对引号整体**识别：引号内的 `#` 属于值、不切；引号外的 `#` 起注释。
# （实测教训：`VS_EMAIL_FORWARDING_IMAGE="has#hash:1.0"` 若先无条件 `${raw%%#*}`，
#  值会被截成 `has`。）取舍：引号外不允许出现裸 `#`——值与注释之间只能是空白；
# 需要值里带 `#` 就用引号包起来。
# 端口那一路再叠一次 `tr -cd '0-9'`，读不到就返回空串、由调用方决定兜底值。
read_env_value() {
    local key="$1" rest line stripped quote
    [ -f ".env" ] || return 0
    line="$(grep -E "^[[:space:]]*${key}[[:space:]]*=" .env | tail -n 1 | cut -d= -f2- || true)"
    rest="${line%$'\r'}"
    rest="${rest#"${rest%%[![:space:]]*}"}" # 去掉左端空白

    stripped=""
    quote=""
    while [ -n "${rest}" ]; do
        case "${rest:0:1}" in
            '"' | "'")
                if [ -z "${quote}" ]; then
                    quote="${rest:0:1}"
                elif [ "${rest:0:1}" = "${quote}" ]; then
                    quote=""
                fi
                ;;
            '#')
                [ -z "${quote}" ] && break
                ;;
        esac
        stripped+="${rest:0:1}"
        rest="${rest:1}"
    done

    stripped="${stripped%"${stripped##*[![:space:]]}"}" # 去掉右端空白
    case "${stripped}" in
        \"*\") stripped="${stripped#\"}" && stripped="${stripped%\"}" ;;
        \'*\') stripped="${stripped#\'}" && stripped="${stripped%\'}" ;;
    esac
    printf '%s' "${stripped}"
}

# 把路径归一化为绝对路径：优先 `readlink -f`（coreutils 的 canonicalize，容忍末段不存在），
# 退回「cd 到所在目录取 pwd」。**不要退回 realpath 无参形态**：它要求路径已存在，
# 相对路径不存在时返回空串（实测 `realpath: sub/compose.yaml: No such file or directory`），
# 会把「路径写错」这件事变成更难判读的空值。
# 与 deploy/migrate.sh 里的同名函数**保持同一手法**（改一处必须同步改另一处）。
absolute_path() {
    local raw="$1"
    if [ "${raw#/}" != "${raw}" ]; then
        printf '%s' "${raw}"
    elif command -v readlink >/dev/null 2>&1 && readlink -f -- "${raw}" >/dev/null 2>&1; then
        readlink -f -- "${raw}"
    else
        printf '%s/%s' "$(pwd)" "${raw#./}"
    fi
}

# 复检一个镜像：在本机存在、且架构 = ${TARGET_PLATFORM}。
# 用途：应用镜像与 compose 依赖的**每一个**镜像都走同一强度，避免「应用镜像查了、
# 依赖镜像没查」留下静默缺口（离线服务器上缺 postgres 镜像时 compose 会去远端拉而失败）。
check_image_arch() {
    local ref="$1" found_arch
    if ! docker image inspect "${ref}" >/dev/null 2>&1; then
        fail "本机找不到镜像 ${ref}。离线服务器上 compose 不会去远端拉镜像：请先把交付的镜像 tar（含基础镜像与 postgres）载入（docker load），或把引用改成已在的 tag。"
    fi
    found_arch="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "${ref}" 2>/dev/null || true)"
    if [ -z "${found_arch}" ]; then
        fail "读不到镜像 ${ref} 的架构：请手动确认 docker image inspect --format '{{.Os}}/{{.Architecture}}' ${ref}。"
    fi
    if [ "${found_arch}" != "${TARGET_PLATFORM}" ]; then
        fail "镜像 ${ref} 的架构是 ${found_arch}，不是 ${TARGET_PLATFORM}：在服务器上会 exec format error。请回产出方重新构建/导出单平台归档（先跑 deploy/check_archive.py 自检）。"
    fi
    info "架构复检通过：${ref} -> ${found_arch}"
}

# --- 0. 前置检查 --------------------------------------------------------------
command -v docker >/dev/null 2>&1 || fail "找不到 docker 命令：本脚本在服务器上用 Docker 起服务。"
docker info >/dev/null 2>&1 || fail "连不上 Docker 守护进程：请确认 dockerd 已启动、当前身份能访问 Docker。"
[ -f "${COMPOSE_FILE}" ] || fail "找不到 compose 文件：${COMPOSE_FILE}（可用 --compose-file 或 COMPOSE_FILE 指定）。"

# 先归一化为绝对路径，再切目录：这样 --compose-file sub/compose.yaml 这类相对路径
# 不会因为 cd 而失效（cd 之后相对路径会按新 CWD 解析，实测会指向不存在的文件）。
COMPOSE_FILE="$(absolute_path "${COMPOSE_FILE}")"
[ -f "${COMPOSE_FILE}" ] || fail "找不到 compose 文件：${COMPOSE_FILE}（可用 --compose-file 或 COMPOSE_FILE 指定）。"
COMPOSE_DIR="$(cd -- "$(dirname -- "${COMPOSE_FILE}")" && pwd)"
cd -- "${COMPOSE_DIR}"
[ -f .env ] || fail "交付目录里没有 .env：本服务不内置任何默认凭据，从 .env.example 复制并填好真实值再跑。"

# compose v2（`docker compose` 子命令）是硬前提：compose.yaml 不含 `version:` 字段，
# 且用了 depends_on.condition: service_healthy，compose v1（python 版）不支持这两点。
docker compose version >/dev/null 2>&1 || fail "找不到 Docker Compose v2（\`docker compose\` 子命令）：本编排文件依赖 compose v2。"

# --- 1. 解析端口：$HTTP_PORT（环境变量）> .env 的 HTTP_PORT > 8000 -------------
if [ -n "${HTTP_PORT:-}" ]; then
    HTTP_PORT="$(printf '%s' "${HTTP_PORT}" | tr -cd '0-9')"
else
    HTTP_PORT="$(read_env_value HTTP_PORT | tr -cd '0-9')"
fi
# 只保留数字，顺带容忍 CR、值两边的引号与行尾注释；读不到就用兜底值。
[ -n "${HTTP_PORT}" ] || HTTP_PORT="${DEFAULT_HTTP_PORT}"
HEALTH_URL="http://127.0.0.1:${HTTP_PORT}/healthz"

# --- 2. 确认应用镜像在本机，并复检架构 ----------------------------------------
IMAGE_REF="$(read_env_value VS_EMAIL_FORWARDING_IMAGE)"
[ -n "${IMAGE_REF}" ] || IMAGE_REF="${DEFAULT_IMAGE_REF}"
info "应用镜像：${IMAGE_REF}"

if ! docker image inspect "${IMAGE_REF}" >/dev/null 2>&1; then
    fail "本机找不到镜像 ${IMAGE_REF}。离线服务器上 compose 不会去远端拉镜像：请先把交付的镜像 tar 载入（docker load），或把 .env 的 VS_EMAIL_FORWARDING_IMAGE 改成已在的 tag。"
fi

check_image_arch "${IMAGE_REF}"

# --- 2b. 复检 compose 依赖的镜像（含基础镜像 postgres:17） ---------------------
# 为什么不止查应用镜像：运行期还依赖 `postgres:17`（基础镜像三件之一，随基础镜像 tar
# 带入）。离线服务器上它缺了/架构不对，compose 会去远端拉而失败——那是最难判读的
# 一类失败。这里让依赖镜像与应用镜像走**同一强度**的检查，也就把 research 01 要求的
# 「load 之后逐 tag inspect 复检架构」在 T-43 范围内落到了实处（收窄时不得删除）。
COMPOSE_IMAGES="$(TARGET_PLATFORM="${TARGET_PLATFORM}" docker compose -f "${COMPOSE_FILE}" config --images 2>/dev/null | sort -u || true)"
COMPOSE_IMAGES="$(printf '%s\n' "${COMPOSE_IMAGES}" | grep -v '^$' | grep -v '^docker build' || true)"
if [ -n "${COMPOSE_IMAGES}" ]; then
    info "compose 声明的镜像（含依赖）逐个复检："
    while IFS= read -r ref; do
        [ -n "${ref}" ] && check_image_arch "${ref}"
    done <<<"${COMPOSE_IMAGES}"
else
    warn "docker compose config --images 没有返回任何镜像引用（compose v2 的该子命令不可用？）：只复检了应用镜像 ${IMAGE_REF}。"
fi

# --- 3. 迁移（默认先迁移，部署顺序固定） --------------------------------------
if [ "${SKIP_MIGRATE}" = "1" ]; then
    warn "按 --skip-migrate 跳过迁移步骤：请自行确认数据库已迁到 head（bash deploy/migrate.sh）。"
else
    info "先跑数据库迁移：bash ${SCRIPT_DIR}/migrate.sh"
    bash "${SCRIPT_DIR}/migrate.sh" --compose-file "${COMPOSE_FILE}"
fi

# --- 4. 起服务 ----------------------------------------------------------------
info "compose 文件：${COMPOSE_FILE}"
info "执行：docker compose -f ${COMPOSE_FILE} up -d"
docker compose -f "${COMPOSE_FILE}" up -d

# --- 5. 健康检查轮询（默认 30 次 × 2s） ---------------------------------------
# 宿主没有 curl 时降级为「在应用容器里用 Python 标准库探测」（镜像里必有 python）。
PROBE_IN_CONTAINER=0
command -v curl >/dev/null 2>&1 || PROBE_IN_CONTAINER=1
if [ "${PROBE_IN_CONTAINER}" = "1" ]; then
    info "宿主没有 curl：降级为在应用容器内用 Python 标准库探测 /healthz。"
fi

echo
info "等待 ${HEALTH_URL} 就绪（最多 $((HEALTH_RETRIES * HEALTH_INTERVAL)) 秒）"

HEALTH_OK=0
attempt=0
while [ "${attempt}" -lt "${HEALTH_RETRIES}" ]; do
    attempt=$((attempt + 1))
    if [ "${PROBE_IN_CONTAINER}" = "1" ]; then
        if docker compose -f "${COMPOSE_FILE}" exec -T app python -c \
            "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)" \
            >/dev/null 2>&1; then
            HEALTH_OK=1
        fi
    elif curl -fsS --max-time 3 -o /dev/null "${HEALTH_URL}" >/dev/null 2>&1; then
        HEALTH_OK=1
    fi

    if [ "${HEALTH_OK}" -eq 1 ]; then
        info "第 ${attempt} 次探测成功：/healthz 返回 200"
        break
    fi
    printf '[信息] 第 %s/%s 次探测未通过，%s 秒后重试\n' "${attempt}" "${HEALTH_RETRIES}" "${HEALTH_INTERVAL}"
    sleep "${HEALTH_INTERVAL}"
done

# --- 6. 容器状态 --------------------------------------------------------------
echo
info "容器状态："
# 用 `sed -n` 截断而不是 `head`：`head` 读满就退出会让 docker 收到 SIGPIPE，
# 在 `set -o pipefail` 下被当成脚本失败（exit 141），出现与容器状态无关的假报错。
docker compose -f "${COMPOSE_FILE}" ps | sed -n '1,30p'

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
       - 数据目录属主不对：chown -R 999:999 ./data/postgres（宿主 SELinux 为
         Enforcing 时还需要给挂载目录打标签，:z / :Z 或等价的 chcon）
       - 端口被占用：ss -lntp | grep ${HTTP_PORT}，或改 .env 里的 HTTP_PORT
       - 防火墙未放行：firewall-cmd --list-ports。放行命令与「运行期间不要执行
         firewall-cmd --reload」的原因见 docs/research/01-delivery-and-deploy.md。
       - 镜像架构不匹配（本脚本已在启动前复检过，若绕过本脚本启动过则再查一次）：
         docker image inspect --format '{{.Os}}/{{.Architecture}}' ${IMAGE_REF}
  4) 回滚：把 .env 里的 VS_EMAIL_FORWARDING_IMAGE 改回上一个版本 tag，再执行本脚本。
EOF
    exit 1
fi

# --- 7. 成功后的提示 ----------------------------------------------------------
cat <<EOF

服务已启动并通过健康检查（${HEALTH_URL} 在约 $(((attempt - 1) * HEALTH_INTERVAL)) 秒内返回 200）。

  查看应用日志 : docker compose -f ${COMPOSE_FILE} logs -f app
  查看全部日志 : docker compose -f ${COMPOSE_FILE} logs -f
  停止服务     : docker compose -f ${COMPOSE_FILE} down
  重启应用     : docker compose -f ${COMPOSE_FILE} restart app

交付验收补充（AC-20 的后半句「运行时不依赖任何外部网络」）：
  健康检查通过后，在一个观察窗口内数应用的**出站连接**：期望为 0。
    ss -Htnp state established '( dport = 5432 )' | wc -l    # 应用连数据库的那几条
    ss -Htnp state established | wc -l                       # 全部已建立连接（应等于上一行）
  两行相等即「除数据库外没有任何已建立连接」——运行时不访问公网，也不访问内网源
  （见 AGENTS.md §8 第 5 条）。注意：容器化部署下 TCP 端口**不按容器身份区分**，
  脚本无法替你判定「哪几条属于应用」，请人工核对。

清场（不留容器/卷/临时 tag；生产数据目录 ./data/postgres 不在清场范围内）：
  docker compose -f ${COMPOSE_FILE} down --remove-orphans

提醒：日志里不应出现邮件正文、收件人地址或凭据（见 AGENTS.md §8 第 4 条）。
EOF
