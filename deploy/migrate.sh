#!/usr/bin/env bash
# =============================================================================
# vs-email-forwarding 数据库迁移入口（部署时执行 Alembic 的**唯一**入口）。
#
# 用途：在交付目录里用应用镜像跑一次性容器，执行
#     docker compose run --rm app uv run alembic -c migrations/alembic.ini upgrade head
#
# 为什么是这三个选择（判据真源：docs/research/01-delivery-and-deploy.md
# 的「待实现脚本必须保留的方法性结论」段与「维护与回滚」段；不在这里重新论证）：
#   * 用 `run --rm` 而**不是**对跑着的容器 `exec`：不依赖应用容器正在运行，也不会把
#     迁移混进服务启动流程（迁移失败时服务不会被「半启动」）；
#   * **不加 `--no-deps`**：让 compose 先把 postgres 等到 healthy，避免「库还没就绪就迁移」；
#   * 应用镜像里带着 uv 与 `migrations/`（见 Dockerfile 的 builder/runtime 两处 COPY），
#     所以离线服务器不需要装 Python / uv、也不需要联网——迁移全程在容器内完成。
#
# ⚠️ 不要放行的一条「预期失败」：镜像/工作目录里**没有 migrations/** 时，命令会报
#    `No 'script_location' key found in configuration.`（实测 exit 255）。这不是「仓库
#    还没有迁移版本」，而是**结构性缺件**，重跑一百次也一样——判读见下面的失败分类①。
#
# 运行期零外网（AGENTS.md §8.5）：`uv run` 默认会按 uv.lock 同步**含 dev 组**的项目环境，
#    容器里没有网 → 迁移与服务双双失败。所以**应用镜像里设了 `UV_NO_SYNC=1`**（见 Dockerfile
#    的 runtime ENV）：运行期 `uv run` 直接用构建期 `uv sync --frozen --no-dev` 装好的 .venv，
#    既不解析也不下载。因此本脚本的命令形状保持 docs/research/01-delivery-and-deploy.md
#    已记录的那一条，**刻意不在这里重复加 `--no-sync`**（重复加会让记录形态与脚本漂移）。
#    反面提醒：`UV_NO_DEV=1` **不是**修法——它只少装 dev 组，uv 仍会去外网解析构建后端。
#    若 .venv 不完整、运行期报 `Failed to spawn:` 一类错误，判读见下面的失败分类④。
#
# 用法（可以从任意目录调用；脚本自己推导默认的 compose.yaml）：
#     bash deploy/migrate.sh
#     bash deploy/migrate.sh --print-command          # 只打印将要执行的命令
#     COMPOSE_FILE=/opt/vs-email-forwarding/compose.yaml bash deploy/migrate.sh
#     bash deploy/migrate.sh --skip-probe             # 跳过「镜像内是否有 migrations/」预检
#     bash deploy/migrate.sh --help
#
# 前置条件：镜像已 load 到本机（`docker image inspect` 能查到
#          `${VS_EMAIL_FORWARDING_IMAGE}`）；交付目录里的 .env 已就位。
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_COMPOSE_FILE="$(cd -- "${SCRIPT_DIR}/.." && pwd)/compose.yaml"
COMPOSE_FILE="${COMPOSE_FILE:-${DEFAULT_COMPOSE_FILE}}"
PRINT_ONLY=0
SKIP_PROBE="${SKIP_PROBE:-0}"

# 迁移命令的形状固定在这里（只有一处），单入口承诺靠它兑现。
ALEMBIC_ARGS=(uv run alembic -c migrations/alembic.ini upgrade head)

# 预检：镜像内是否真的带着迁移定义。只读、不执行 alembic，故不会触网也不会改库。
PROBE_SCRIPT="import os,sys;p='/app/migrations';n=len([f for f in os.listdir(p+'/versions') if f.endswith('.py')]) if os.path.isdir(p+'/versions') else -1;print('migrations_versions='+str(n));sys.exit(0 if os.path.isfile(p+'/alembic.ini') and os.path.isfile(p+'/env.py') else 1)"
PROBE_ARGS=(python -c "${PROBE_SCRIPT}")

info() { echo "[信息] $*"; }
warn() { echo "[警告] $*" >&2; }
fail() { echo "[失败] $*" >&2; exit 1; }

usage() {
    cat <<'EOF'
用法：bash deploy/migrate.sh [选项]

在应用镜像里跑一次性容器执行 Alembic 迁移（`upgrade head`），是部署时的唯一迁移入口。

选项：
  -h, --help            显示本说明
      --print-command   只打印将要执行的命令，不真的执行（用于交付前预演/评审）
      --compose-file F  指定 compose 文件（默认：本脚本上一级的 compose.yaml；
                        也可以用环境变量 COMPOSE_FILE 指定）
      --skip-probe      跳过「镜像内是否有 migrations/」预检，直接跑迁移
                        （环境变量 SKIP_PROBE=1 同效）

环境变量：
  COMPOSE_FILE  同 --compose-file
  SKIP_PROBE    置 1 等同 --skip-probe

退出码：
  0 = 迁移成功
  1 = 前置条件不满足，或迁移命令失败（失败性质由下面的失败分类判读）

失败分类（判据，不是「可能原因」清单）：
  ① 结构性故障：镜像/工作目录里没有 migrations/。
     判别式 = 预检报告 migrations/alembic.ini 或 env.py 缺失，或 alembic 报的正是
     `No 'script_location' key found in configuration.` / 找不到 alembic.ini 这一族。
     这不是「仓库还没有迁移版本」的预期失败：缺配置不会自己好。
     排查：docker image inspect <镜像> --format '{{.Config.Cmd}}'
           docker compose run --rm app ls /app/migrations
     修复：重新构建/载入带 migrations/ 的镜像（Dockerfile 的两处 COPY）。
  ② 连接/库级故障：报连接数据库失败、认证失败、库不存在。
     检查 .env 的 POSTGRES_* 与 DATABASE_URL 是否一致，并确认 postgres 容器 healthy：
       docker compose -f <compose> ps
  ③ 迁移脚本自身错误（SQL 语法、约束冲突、重复对象等）：修好后重跑本脚本，
     `upgrade head` 是幂等的。
  ※ 「无版本 ⇒ 首次迁移必然失败」只在**能给出判别式**时才成立：预检确认镜像内
     alembic.ini 与 env.py 都在，而 alembic 明确报「没有 revision」。当前仓库的
     migrations/versions/ 已有版本，因此这条路径基本不再适用。
EOF
}

# 把路径归一化为绝对路径：优先 `readlink -f`（coreutils 的 canonicalize，容忍末段不存在），
# 退回「cd 到所在目录取 pwd」。**不要退回 realpath 无参形态**：它要求路径已存在，
# 相对路径不存在时返回空串，会把「路径写错」变成更难判读的空值。
# 与 deploy/run.sh 里的同名函数**保持同一手法**（改一处必须同步改另一处）。
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

# 读交付目录 .env 里的一个键（用于把「真实镜像引用」告诉运维）。
# 手法与 deploy/run.sh 的 read_env_value **逐字同形**（改一处必须同步改另一处）：
# 容忍 CRLF、值两边空白、成对引号，以及引号**外**的行尾注释；引号内的 # 属值不截断。
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

# 解析参数：只认这几条，其余一律拒绝（避免运维以为传了参数、其实被静默忽略）。
while [ "$#" -gt 0 ]; do
    case "$1" in
        -h | --help)
            usage
            exit 0
            ;;
        --print-command)
            PRINT_ONLY=1
            shift
            ;;
        --skip-probe)
            SKIP_PROBE=1
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

command -v docker >/dev/null 2>&1 || fail "找不到 docker 命令：本脚本在服务器上用 Docker 跑迁移。"
docker info >/dev/null 2>&1 || fail "连不上 Docker 守护进程：请确认 dockerd 已启动、当前身份能访问 Docker。"

# 先归一化为绝对路径，再切目录：这样 --compose-file sub/compose.yaml 这类相对路径
# 不会因为 cd 而失效（cd 之后相对路径会按新 CWD 解析）。
COMPOSE_FILE="$(absolute_path "${COMPOSE_FILE}")"
[ -f "${COMPOSE_FILE}" ] || fail "找不到 compose 文件：${COMPOSE_FILE}（可用 --compose-file 或 COMPOSE_FILE 指定）。"

# compose 的 .env 插值、相对路径挂载都按「项目目录」解析：先切过去再执行。
COMPOSE_DIR="$(cd -- "$(dirname -- "${COMPOSE_FILE}")" && pwd)"
cd -- "${COMPOSE_DIR}"

info "compose 文件：${COMPOSE_FILE}"

# 真实镜像引用：优先 shell 环境变量（compose 就是按这个优先级插值的），其次交付目录 .env，
# 都没有就留空——排查提示里明说「未设置」，绝不拿 `:latest` 兜底（运维 shell 取不到 .env
# 变量时，去 inspect 一个并不存在的 latest 只会把人带偏）。
IMAGE_REF="${VS_EMAIL_FORWARDING_IMAGE:-}"
if [ -z "${IMAGE_REF}" ]; then
    IMAGE_REF="$(read_env_value VS_EMAIL_FORWARDING_IMAGE)"
fi

info "执行：docker compose -f ${COMPOSE_FILE} run --rm app ${ALEMBIC_ARGS[*]}"

if [ "${PRINT_ONLY}" -eq 1 ]; then
    info "--print-command：不真的执行，到此为止。"
    exit 0
fi

# --- 预检：镜像内是否真的有 migrations/ ----------------------------------------
# 目的：把「结构性缺件」与「迁移脚本自身错误」分开，避免把缺 migrations/ 的镜像
# 误报成「仓库还没有迁移版本，属预期」。预检会跑一次一次性容器（成本约 1 秒，也可能
# 触发 compose 依赖，与迁移本身同形）；预检自身失败不阻断流程——真正的判据在失败分类里。
MIGRATIONS_VERSIONS="?"
MIGRATIONS_MISSING=0
if [ "${SKIP_PROBE}" = "1" ]; then
    warn "按 --skip-probe 跳过「镜像内是否有 migrations/」预检；失败分类①仍会照常判读。"
else
    set +e
    PROBE_LOG="$(docker compose -f "${COMPOSE_FILE}" run --rm app "${PROBE_ARGS[@]}" 2>&1)"
    PROBE_STATUS=$?
    set -e
    case "${PROBE_LOG}" in
        *migrations_versions=*)
            MIGRATIONS_VERSIONS="${PROBE_LOG##*migrations_versions=}"
            MIGRATIONS_VERSIONS="${MIGRATIONS_VERSIONS%%[!0-9-]*}"
            ;;
    esac
    if [ "${PROBE_STATUS}" -ne 0 ]; then
        MIGRATIONS_MISSING=1
        warn "预检：应用镜像里没有 migrations/（或缺少 alembic.ini / env.py）——见失败分类①。"
    else
        info "预检：镜像内 migrations/ 就位（versions 下 .py 文件数=${MIGRATIONS_VERSIONS}）。"
    fi
fi

# --- 执行迁移 ------------------------------------------------------------------
# 输出先整体收进变量：一是失败时要判读文案，二是要控制回显长度。管道里只有 printf，
# 不涉及 docker（docker 的输出已经完整收在变量里），所以不存在 SIGPIPE 顾虑。
set +e
MIGRATE_LOG="$(docker compose -f "${COMPOSE_FILE}" run --rm app "${ALEMBIC_ARGS[@]}" 2>&1)"
MIGRATE_STATUS=$?
set -e

# 回显只取**尾部**：Alembic 的结论（跑到哪个 revision、失败原因）都在末尾，
# 留头部会把真正的判据截掉。
printf '%s\n' "${MIGRATE_LOG}" | tail -n 120

if [ "${MIGRATE_STATUS}" -eq 0 ]; then
    info "迁移完成（已到 head）。下一步：bash ${SCRIPT_DIR}/run.sh"
    exit 0
fi

PROBE_CONCLUSION="预检被跳过，无法据此判断镜像内是否有 migrations/"
if [ "${SKIP_PROBE}" != "1" ]; then
    if [ "${MIGRATIONS_MISSING}" = "1" ]; then
        PROBE_CONCLUSION="预检报告缺件（migrations/alembic.ini 或 env.py 不在镜像里）"
    else
        PROBE_CONCLUSION="预检报告 migrations/ 就位（versions 下 .py 文件数=${MIGRATIONS_VERSIONS}）"
    fi
fi

# 排查命令里的镜像引用用**实际值**（shell 导出优先，其次交付目录 .env），
# 解析不到就明说「未设置」，绝不用 `:latest` 兜底（那会把运维带到一个并不存在的 tag）。
if [ -n "${IMAGE_REF}" ]; then
    INSPECT_HINT="docker image inspect ${IMAGE_REF} --format '{{.Config.Cmd}}'"
    VENV_HINT="docker run --rm --entrypoint ls ${IMAGE_REF} /app/.venv/bin/alembic"
    ENV_HINT="docker image inspect ${IMAGE_REF} --format '{{json .Config.Env}}'"
else
    INSPECT_HINT="（未知：shell 未导出 VS_EMAIL_FORWARDING_IMAGE，交付目录 .env 里也没有该键。
             先用 docker compose -f ${COMPOSE_FILE} config --images 取出真实引用，
             再 docker image inspect <引用> --format '{{.Config.Cmd}}'）"
    VENV_HINT="docker run --rm --entrypoint ls <镜像引用> /app/.venv/bin/alembic"
    ENV_HINT="docker image inspect <镜像引用> --format '{{json .Config.Env}}'"
fi

cat >&2 <<EOF

[失败] 迁移未成功（退出码 ${MIGRATE_STATUS}）。按下面的失败分类判读：

  ① 结构性故障 —— 镜像/工作目录里没有 migrations/。
     本次${PROBE_CONCLUSION}。
     判别式：预检报告缺件，或 alembic 报 \`No 'script_location' key found in configuration.\` /
             找不到 alembic.ini 这一族（这一族**不是**「还没有迁移版本」——缺配置不会自己好）。
     排查两条：
       ${INSPECT_HINT}
       docker compose -f ${COMPOSE_FILE} run --rm app ls /app/migrations
     修复：重新构建/载入带 migrations/ 的镜像（Dockerfile 的 builder/runtime 各一处 COPY），
           并确认交付输入里含 migrations/（判据见 docs/research/01-delivery-and-deploy.md
           的「维护与回滚」段）。

  ② 连接/库级故障：报连接数据库失败、认证失败、库不存在。
     检查 .env 里的 POSTGRES_* 与 DATABASE_URL 是否一致，并确认 postgres 容器已经 healthy：
       docker compose -f ${COMPOSE_FILE} ps

  ③ 迁移脚本自身错误（SQL 语法、约束冲突、重复对象等）：修好后直接重跑本脚本，
     \`upgrade head\` 是幂等的；修好后直接重跑本脚本是安全的。

  ④ 运行环境故障 —— **镜像构建期 uv sync 未生效**（.venv 不完整）：不是网络、也不是配置问题。
     判别式（命中任一条即归此类）：
       * 报 \`Failed to spawn:\`（如 Failed to spawn: \`alembic\` / No such file or directory)
         —— uv 在 .venv 里找不到要跑的命令；
       * 报 \`Creating virtual environment\` / \`failed to create directory\` 这类「运行期现造
         环境」的字样；
       * 报 \`Failed to download\` / \`dns error\` —— 那是 uv 的隐式同步又回来了（本镜像已设
         UV_NO_SYNC=1 关掉同步；出现这类报错时先按下面第二条排查确认它是否还在位）。
     排查两条：
        ${VENV_HINT}
        ${ENV_HINT}     # 应当含 UV_NO_SYNC=1
     修复：在**有网的构建机**上重建镜像并重新导出（构建期 \`uv sync --frozen --no-dev\` 必须
           成功）；不要在服务器上装包——AGENTS.md §8.5 禁止运行期依赖任何外部网络。
     反面提醒：\`UV_NO_DEV=1\` 不是修法（只少装 dev 组，uv 仍会去外网解析构建后端）。

  另：「无版本 ⇒ 首次迁移必然失败」只在**能给出判别式**时才成立 —— 预检确认镜像内
      alembic.ini 与 env.py 都在，而 alembic 明确报「没有 revision」。当前仓库
      migrations/versions/ 已有版本，这条路径基本不再适用。

  需要先跳过迁移把服务起起来时，用 \`bash ${SCRIPT_DIR}/run.sh --skip-migrate\`——**不要**直接跑
  不带参数的 \`bash ${SCRIPT_DIR}/run.sh\`，它默认会再跑一遍本脚本，等于原地打转。

  本次回显只保留了输出**尾部 120 行**（Alembic 的结论在末尾）。截断只是控制回显长度，
  **不能替代脱敏**：日志或贴出内容前，仍要按 AGENTS.md §8 第 4 条核对不含正文、地址与凭据。
EOF
exit 1
