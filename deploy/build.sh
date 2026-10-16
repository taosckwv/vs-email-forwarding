#!/usr/bin/env bash
# =============================================================================
# vs-email-forwarding 产出侧构建脚本：在**开发机**上构建应用镜像。
#
# 为什么构建发生在开发机（判据真源：docs/research/01-delivery-and-deploy.md 的「结论」段）：
#   * 生产服务器与公网隔离，而构建期 `uv sync` 需要包源；仓库里没有任何内网源配置，
#     这条路从未跑通——把构建留在服务器，等于把「装依赖」这一步的失败面留在生产；
#   * 开发机 Docker 与生产**同版本、同 image store 模式**（26.1.3 / overlay2），
#     构建产物与生产加载侧的兼容性风险最低；
#   * 服务器因此不再执行任何构建：只 `docker load` 应用镜像 tar 与 postgres:17 tar
#     （见 deploy/package.sh）。
#
# 做的事（顺序固定）：
#     1) 前置检查：在仓库里、`Dockerfile` 与 `pyproject.toml` 就位、docker 可用；
#     2) `docker build --platform linux/amd64`——**平台只在这一步钉死**
#        （compose.yaml 里的 `platform` 只是运行期保险，不决定架构）；
#     3) 构建后复检：镜像必须是 `linux/amd64`；
#     4) 打印 tag / image id / 大小，并指向下一步 `deploy/package.sh`。
#
# 用法：
#     bash deploy/build.sh                       # tag 取 pyproject.toml 的 version
#     bash deploy/build.sh --tag vs-email-forwarding:0.1.1
#     bash deploy/build.sh --no-cache            # 验证一次干净构建
#     bash deploy/build.sh --print-command       # 只打印命令，不执行
#     bash deploy/build.sh --help
#
# 前置条件：本机 docker 可用；Dockerfile 里两个构建期基础镜像**已按 digest 钉死**，
#          本机需已 `docker load` 或能拉到它们（开发机有公网，见 research 05）。
#          本脚本会**显式拒绝** `DOCKER_BUILDKIT=0`（理由见下方拦截处）。
#
# 退出码：0 = 构建成功且架构正确；1 = 前置条件、构建或复检失败。
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

TARGET_PLATFORM="linux/amd64"
IMAGE_NAME="vs-email-forwarding"
DOCKERFILE="${REPO_ROOT}/Dockerfile"
PYPROJECT="${REPO_ROOT}/pyproject.toml"

TAG=""
NO_CACHE=0
PRINT_ONLY=0

info() { echo "[信息] $*"; }
warn() { echo "[警告] $*" >&2; }
fail() { echo "[失败] $*" >&2; exit 1; }

usage() {
    cat <<'EOF'
用法：bash deploy/build.sh [选项]

在开发机上构建应用镜像（docker build --platform linux/amd64），并在构建后复检架构。
服务器侧不执行构建：它只 load 本脚本与 deploy/package.sh 产出的 tar。

选项：
  -h, --help        显示本说明
      --tag TAG     镜像 tag（默认：vs-email-forwarding:<pyproject.toml 的 version>）
      --no-cache    构建时加 --no-cache（用于验证一次干净构建）
      --print-command  只打印将要执行的 docker build 命令，不执行

退出码：
  0 = 构建成功，且镜像架构为 linux/amd64
  1 = 前置条件不满足、构建失败，或架构复检不通过
EOF
}

# 读 [project] version 作为默认 tag。用 python3 的 tomllib 而不是 grep：
# pyproject.toml 里 commitizen 段也有 `version =`，按行拍字符串迟早取错一处。
read_project_version() {
    python3 - "${PYPROJECT}" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as handle:
    print(tomllib.load(handle)["project"]["version"])
PY
}

# 解析参数：只认这几条，其余一律拒绝（避免以为传了参数、其实被静默忽略）。
while [ "$#" -gt 0 ]; do
    case "$1" in
        -h | --help)
            usage
            exit 0
            ;;
        --tag)
            [ "$#" -ge 2 ] || fail "--tag 需要一个参数（镜像 tag）。"
            TAG="$2"
            shift 2
            ;;
        --no-cache)
            NO_CACHE=1
            shift
            ;;
        --print-command)
            PRINT_ONLY=1
            shift
            ;;
        *)
            fail "不认识的参数：$1（用 --help 看用法）。"
            ;;
    esac
done

[ -f "${DOCKERFILE}" ] || fail "找不到 ${DOCKERFILE}：请在仓库里运行本脚本。"
[ -f "${PYPROJECT}" ] || fail "找不到 ${PYPROJECT}：请在仓库里运行本脚本。"
command -v docker >/dev/null 2>&1 || fail "本机没有 docker，无法构建。"

# BuildKit 的显式拦截（判据出处：docs/research/03-dependencies-and-toolchain.md 的
# 「构建环境事实」段）：`--platform` 的平台语义由 BuildKit 保证，DOCKER_BUILDKIT=0 会关掉它，
# 于是可能**静默产出错架构的镜像**。检测到就立即失败——不得降级继续构建。
if [ "${DOCKER_BUILDKIT:-1}" = "0" ]; then
    fail "检测到 DOCKER_BUILDKIT=0：--platform 的平台语义由 BuildKit 保证，关掉它可能静默产出错架构镜像。请 unset DOCKER_BUILDKIT 后重跑。"
fi

if [ -z "${TAG}" ]; then
    version="$(read_project_version)" || fail "读不出 ${PYPROJECT} 的 [project] version（需要 python3 的 tomllib）。"
    [ -n "${version}" ] || fail "${PYPROJECT} 的 [project] version 是空的。"
    TAG="${IMAGE_NAME}:${version}"
fi

build_cmd=(docker build --platform "${TARGET_PLATFORM}" --file "${DOCKERFILE}" --tag "${TAG}")
[ "${NO_CACHE}" -eq 1 ] && build_cmd+=(--no-cache)
build_cmd+=("${REPO_ROOT}")

info "仓库根：${REPO_ROOT}"
info "构建命令：${build_cmd[*]}"
if [ "${PRINT_ONLY}" -eq 1 ]; then
    exit 0
fi

"${build_cmd[@]}" || fail "docker build 失败（基础镜像是否已按 Dockerfile 里钉的 digest 备好？）。"

image_id="$(docker image inspect "${TAG}" --format '{{.Id}}')"
platform="$(docker image inspect "${TAG}" --format '{{.Os}}/{{.Architecture}}')"
size_bytes="$(docker image inspect "${TAG}" --format '{{.Size}}')"

if [ "${platform}" != "${TARGET_PLATFORM}" ]; then
    fail "镜像 ${TAG} 的架构是 ${platform}，不是 ${TARGET_PLATFORM}：载入服务器后会 exec format error。构建时必须带 --platform ${TARGET_PLATFORM}。"
fi

info "构建完成：${TAG}"
info "  image id ：${image_id}"
info "  平台     ：${platform}"
info "  大小     ：$(awk -v bytes="${size_bytes}" 'BEGIN { printf "%.0f MB", bytes / 1024 / 1024 }')"
info "下一步：bash deploy/package.sh            # 首次交付：应用镜像 + postgres:17 两件都打"
info "        bash deploy/package.sh --app-only # 日常发版：只重打应用镜像"
