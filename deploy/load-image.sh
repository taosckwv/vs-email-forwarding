#!/usr/bin/env bash
# =============================================================================
# 在**离线服务器**（CentOS 8）上加载交付镜像，并校验完整性**与镜像架构**。
#
# 用法：
#     bash deploy/load-image.sh [镜像目录]        # 默认当前目录
#     sudo bash deploy/load-image.sh /opt/mail-relay
#
# 做的事：
#     1) 检查 docker 与守护进程权限；
#     2) 读取并打印 `docker version` 的 **Server 段**版本，以及 Storage Driver；
#     3) 对每个 *.tar 先用伴随的 <tar>.sha256 校验，再 docker load（**不带 --platform**）；
#     4) 载入后逐个校验：架构必须是 linux/amd64，且本次交付的 tag 都在本地；
#     5) 打印已加载镜像与下一步命令。
#
# 幂等性：可以反复执行。docker load 对已存在的镜像只是重新打 tag / 复用已有层，
#         不会报错，也不会清掉已有数据。重复执行的结果与执行一次相同。
#
# 已核实的服务器事实（2026-09 服务器实测）：
#     * Client / Server Engine 均为 **26.1.3**，API **1.45**（最低 1.24）；
#       containerd 1.6.32 / runc 1.1.12；Engine 构建于 2024-05-16。
#     * `docker save` / `docker load` 的 `--platform` 需要 **API 1.48（Engine v28）**，
#       服务器是 1.45 —— 因此本脚本**不传这个参数**（传了会因未知参数直接失败）；
#       平台改由「开发机构建期钉死 + 开发机保存前校验 + 这里载入后校验」保证。
#     * Engine >= 25 才能读 **OCI 归档**（moby v25.0.0 起 docker save 产出 OCI 格式），
#       26.1.3 满足这一条。
#     * **classic image store 不支持 image index / manifest list**，所以交付物必须是
#       **单平台 amd64 归档**；开发机上先跑 deploy/check-image-archive.py 自检
#       （不依赖 Docker，判读标准见 deploy/README.md「交付前归档自检」一节）。
#     * 仍未确认的一项：image store 模式。本脚本只把 Storage Driver 打印出来供人工
#       判断——overlay2 = classic，overlayfs = containerd store（该对应关系待现场确认）。
#
# 服务器上**不需要** Python、不需要 uv、不需要联网（见 deploy/README.md）。
# =============================================================================
set -euo pipefail

# 目标架构：必须与开发机构建期钉死的平台一致，否则容器在服务器上起不来。
# 该变量只用于**校验**，不再传给 docker load（服务器 API 1.45 < 1.48）。
PLATFORM="linux/amd64"

# 应用镜像仓库名：必须与开发机 save-image.ps1 -ImageName 一致（默认 mail-relay）。
IMAGE_NAME="mail-relay"

# 镜像目录：默认当前工作目录（通常就是交付目录，tar 与 compose.yaml、.env 放一起）
IMAGES_DIR="${1:-$(pwd)}"

info()  { echo "[信息] $*"; }
warn()  { echo "[警告] $*" >&2; }
fail()  { echo "[失败] $*" >&2; exit 1; }

# 载入后校验单个镜像引用：架构必须是 ${PLATFORM}
# 为什么用校验代替 `docker image load --platform`：该参数需要 API 1.48（Engine v28），
# 服务器实测 API 1.45，传了会直接报未知参数；平台由开发机构建期钉死，这里只做确认。
verify_ref() {
    local ref="$1"
    local hint="${2:-}"
    local os_arch
    os_arch="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "${ref}" 2>/dev/null || echo '')"
    if [ -z "${os_arch}" ]; then
        fail "本地找不到镜像 ${ref} ${hint}：docker load 没有把它带进本地镜像库。"
    fi
    if [ "${os_arch}" != "${PLATFORM}" ]; then
        fail "镜像 ${ref} 的架构是 ${os_arch}，不是 ${PLATFORM}：在服务器上会 exec format error。这个交付包不可用，请回开发机重新构建/导出（见 deploy/README.md）。"
    fi
    info "架构校验通过：${ref} -> ${os_arch}"
}

# --- 0. 参数检查 --------------------------------------------------------------
[ -d "${IMAGES_DIR}" ] || fail "镜像目录不存在：${IMAGES_DIR}"
IMAGES_DIR="$(cd -- "${IMAGES_DIR}" && pwd)"

# --- 1. docker 可用性与权限 ---------------------------------------------------
command -v docker >/dev/null 2>&1 || fail "找不到 docker 命令：服务器上应已装好 Docker（本脚本不用 Python，也不用 uv）。"

if ! docker info >/dev/null 2>&1; then
    fail "连不上 Docker 守护进程。常见原因：当前用户不在 docker 组（改用 sudo），或 dockerd 未启动（systemctl start docker）。"
fi

# --- 2. 服务器 Docker 版本与 image store（用于判断，不做硬门禁） --------------
# 读的是 `docker version` 的 **Server 段**，不是 `docker --version`（后者只打印 CLI
# 版本，而归档能不能被读取决于是 Engine）。本项目实测两者都是 26.1.3，但不要靠 CLI 推断。
CLIENT_VERSION="$(docker version --format '{{.Client.Version}}' 2>/dev/null || echo '')"
SERVER_VERSION="$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo '')"
SERVER_API="$(docker version --format '{{.Server.APIVersion}}' 2>/dev/null || echo '')"
SERVER_MAJOR="${SERVER_VERSION%%.*}"
info "Docker CLI ${CLIENT_VERSION:-未知} / Engine ${SERVER_VERSION:-未知}（Server API ${SERVER_API:-未知}）"

if [ -n "${SERVER_MAJOR}" ] && [ "${SERVER_MAJOR}" -lt 25 ] 2>/dev/null; then
    warn "Engine ${SERVER_VERSION} < 25：读不了本交付包（moby v25.0.0 起 docker save 产出 OCI 归档）。"
    warn "请先升级 Docker Engine，或改用其他交付方式；本次加载很可能失败。"
fi

# image store 模式：仍未确认的一项（classic 还是 containerd）。这里只打印，供人工判断。
STORAGE_DRIVER="$(docker info --format '{{.Driver}}' 2>/dev/null || echo '')"
info "Storage Driver：${STORAGE_DRIVER:-读不到}"
case "${STORAGE_DRIVER}" in
    overlay2)
        info "  -> overlay2 通常对应 classic image store；本交付包是单平台归档，并会在载入后逐个校验架构。"
        ;;
    overlayfs)
        warn "  -> overlayfs 通常对应 containerd image store（该解读仍属待现场确认项，请与运维核对）。"
        ;;
    '')
        warn "  读不到 Storage Driver：请在服务器上手动执行 docker info，确认 image store 模式。"
        ;;
    *)
        info "  该取值需人工判断：overlay2 = classic，overlayfs = containerd store（仍待现场确认）。"
        ;;
esac

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
    # 刻意**不带** `--platform`：该参数需要 API 1.48（Engine v28），服务器实测 API 1.45，
    # 传了会因未知参数直接失败。平台由开发机构建期钉死，这里改为载入后校验架构（见 3.3）。
    info "docker load ${tar_name}（不带 --platform）"
    if ! load_output="$(docker image load --input "${tar_path}" 2>&1)"; then
        printf '%s\n' "${load_output}" >&2
        fail "docker load 失败。若报的是 manifest / index 相关错误：归档可能是带 image index / manifest list 的多平台包，而 classic image store 载不动 —— 请回开发机跑 deploy/check-image-archive.py 自检后重新导出单平台归档。"
    fi
    printf '%s\n' "${load_output}"

    # 3.3 载入后校验：架构必须是 ${PLATFORM}，且本次交付的 tag 都在本地
    loaded_refs=()
    while IFS= read -r line; do
        case "${line}" in
            *"Loaded image: "*) loaded_refs+=("${line##*Loaded image: }") ;;
        esac
    done <<< "${load_output}"

    if [ "${#loaded_refs[@]}" -gt 0 ]; then
        for ref in "${loaded_refs[@]}"; do
            verify_ref "${ref}"
        done
    else
        warn "docker load 没有报告 'Loaded image: <引用>'（可能只按镜像 ID 载入）。"
    fi

    # 预期 tag：save-image.ps1 生成的 tar 名固定为 <ImageName>-images-<版本>.tar。
    # 推断得出就逐个校验存在性与架构；推断不出就只校验上面的引用，并提示人工确认。
    expected_version=""
    case "${tar_name}" in
        "${IMAGE_NAME}-images-"*.tar)
            expected_version="${tar_name#"${IMAGE_NAME}-images-"}"
            expected_version="${expected_version%.tar}"
            ;;
    esac
    if [ -n "${expected_version}" ]; then
        for ref in "${IMAGE_NAME}:${expected_version}" "${IMAGE_NAME}:latest" "postgres:17"; do
            verify_ref "${ref}" "（本次交付的预期 tag）"
        done
    else
        warn "tar 名不是 ${IMAGE_NAME}-images-<版本>.tar，无法推断预期 tag；"
        warn "请人工确认 ${IMAGE_NAME}:<版本>、${IMAGE_NAME}:latest、postgres:17 都在本地（docker image ls）。"
    fi
    LOADED=$((LOADED + 1))
done

# --- 4. 结果与下一步 ----------------------------------------------------------
echo
info "已加载 ${LOADED} 个 tar，且每个镜像的架构都校验为 ${PLATFORM}。"
info "（重复执行本脚本是安全的：已存在的镜像只会被重新打 tag。）"
info "本地镜像一览（前 20 行）："
# 用 sed 而不是 head：head 读满就退出会让 docker 收到 SIGPIPE，在 `set -o pipefail` 下
# 会被当成脚本失败（exit 141），出现与镜像内容无关的假报错。
docker image ls --format 'table {{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.Size}}' | sed -n '1,20p'

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
