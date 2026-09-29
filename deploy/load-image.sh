#!/usr/bin/env bash
# =============================================================================
# 在**离线服务器**（CentOS 8）上加载交付镜像并校验完整性。
#
# 用法：
#     bash deploy/load-image.sh [镜像目录]        # 默认当前目录
#     sudo bash deploy/load-image.sh /opt/mail-relay
#
# 做的事：
#     1) 检查 docker 与守护进程权限；
#     2) 检查 Engine/CLI 版本是否满足要求（见下）；
#     3) 对每个 *.tar 先用伴随的 <tar>.sha256 校验，再 docker load；
#     4) 打印已加载镜像与下一步命令。
#
# 幂等性：可以反复执行。docker load 对已存在的镜像只是重新打 tag / 复用已有层，
#         不会报错，也不会清掉已有数据。重复执行的结果与执行一次相同。
#
# 版本要求（**上线前必须核对**，见 deploy/README.md）：
#     * 自 moby v25.0.0 起，docker save 产出 **OCI 归档**；
#     * Docker Desktop 与 Engine >= 29 默认使用 containerd image store，而
#       **classic image store 不支持 image index / manifest list**，无法 load 多平台镜像；
#     * `--platform` 参数要到 Engine **v28** 才有。
#     所以本脚本会读 docker 版本：Engine < 25 或 CLI < 28 时给出明确警告，
#     CLI < 28 时自动退化为不带 --platform 的 load（单平台归档通常仍能 load）。
#
# 服务器上**不需要** Python、不需要 uv、不需要联网（见 deploy/README.md）。
# =============================================================================
set -euo pipefail

# 镜像架构：必须与开发机 build/save 的平台一致，否则服务器上起不来。
PLATFORM="linux/amd64"

# 镜像目录：默认当前工作目录（通常就是交付目录，tar 与 compose.yaml、.env 放一起）
IMAGES_DIR="${1:-$(pwd)}"

info()  { echo "[信息] $*"; }
warn()  { echo "[警告] $*" >&2; }
fail()  { echo "[失败] $*" >&2; exit 1; }

# --- 0. 参数检查 --------------------------------------------------------------
[ -d "${IMAGES_DIR}" ] || fail "镜像目录不存在：${IMAGES_DIR}"
IMAGES_DIR="$(cd -- "${IMAGES_DIR}" && pwd)"

# --- 1. docker 可用性与权限 ---------------------------------------------------
command -v docker >/dev/null 2>&1 || fail "找不到 docker 命令：服务器上应已装好 Docker（本脚本不用 Python，也不用 uv）。"

if ! docker info >/dev/null 2>&1; then
    fail "连不上 Docker 守护进程。常见原因：当前用户不在 docker 组（改用 sudo），或 dockerd 未启动（systemctl start docker）。"
fi

# --- 2. 版本核对 --------------------------------------------------------------
CLIENT_VERSION="$(docker version --format '{{.Client.Version}}' 2>/dev/null || echo '')"
SERVER_VERSION="$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo '')"
CLIENT_MAJOR="${CLIENT_VERSION%%.*}"
SERVER_MAJOR="${SERVER_VERSION%%.*}"
info "Docker CLI ${CLIENT_VERSION:-未知} / Engine ${SERVER_VERSION:-未知}"

USE_PLATFORM_FLAG=1
if [ -n "${SERVER_MAJOR}" ] && [ "${SERVER_MAJOR}" -lt 25 ] 2>/dev/null; then
    warn "Engine ${SERVER_VERSION} < 25：本交付包由新版 docker save 生成（OCI 归档），"
    warn "该引擎可能无法 load。请先升级 Docker Engine，或改用其他交付方式。"
fi
if [ -n "${CLIENT_MAJOR}" ] && [ "${CLIENT_MAJOR}" -lt 28 ] 2>/dev/null; then
    warn "Docker CLI ${CLIENT_VERSION} < 28：不支持 \`docker image load --platform\`，本次将不带该参数加载。"
    warn "请人工确认加载出来的镜像架构是 ${PLATFORM}（docker image inspect --format '{{.Os}}/{{.Architecture}}' <镜像>）。"
    USE_PLATFORM_FLAG=0
fi

# --- 3. 逐个 tar 校验 + load --------------------------------------------------
shopt -s nullglob
TARS=("${IMAGES_DIR}"/*.tar)
shopt -u nullglob

if [ "${#TARS[@]}" -eq 0 ]; then
    fail "目录 ${IMAGES_DIR} 下没有找到 *.tar。请确认 tar 已经通过介质/跳板机传上来。"
fi

info "在 ${IMAGES_DIR} 下找到 ${#TARS[@]} 个 tar，开始校验并加载。"
LOADED=0

for tar_path in "${TARS[@]}"; do
    tar_name="$(basename -- "${tar_path}")"
    echo
    info "---- ${tar_name} ----"

    # 3.1 sha256 校验（传输完整性；缺校验文件时只打印算出来的值，供人工比对）
    sha_file="${tar_path}.sha256"
    if [ -f "${sha_file}" ]; then
        info "校验 sha256（依据 ${tar_name}.sha256）"
        if ! ( cd -- "${IMAGES_DIR}" && sha256sum -c --status "${tar_name}.sha256" ); then
            fail "sha256 校验失败：${tar_name} 在传输过程中损坏或被篡改。请重新传输，不要加载这个包。"
        fi
        info "sha256 校验通过"
    else
        warn "缺少 ${tar_name}.sha256，跳过自动校验。实际 sha256 如下，请与开发机输出人工比对："
        sha256sum "${tar_path}" >&2
    fi

    # 3.2 docker load
    info "docker load ${tar_name}"
    if [ "${USE_PLATFORM_FLAG}" -eq 1 ]; then
        docker image load --platform "${PLATFORM}" --input "${tar_path}"
    else
        docker image load --input "${tar_path}"
    fi
    LOADED=$((LOADED + 1))
done

# --- 4. 结果与下一步 ----------------------------------------------------------
echo
info "已加载 ${LOADED} 个 tar（重复执行本脚本是安全的：已存在的镜像只会被重新打 tag）。"
info "本地镜像一览（截取前 20 行）："
docker image ls --format 'table {{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.Size}}' | head -n 20

cat <<'EOF'

下一步：
  1) 准备 .env：从 .env.example 复制并填真实值（数据库口令、SMTP/IMAP 账号等），
     并把 MAIL_RELAY_IMAGE 指向本次交付的版本 tag（例如 mail-relay:0.2.0）。
  2) 初始化数据目录属主（首次部署）：
       mkdir -p ./data/postgres && chown -R 999:999 ./data/postgres
  3) 跑数据库迁移：bash deploy/migrate.sh
     （Alembic 尚未初始化时这一步会失败，属预期，见脚本内注释。）
  4) 起服务并健康检查：bash deploy/run.sh
EOF
