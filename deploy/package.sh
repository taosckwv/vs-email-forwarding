#!/usr/bin/env bash
# =============================================================================
# vs-email-forwarding 产出侧打包脚本：把要带入服务器的镜像打成 tar，逐件做归档自检、
# 生成 sha256 旁路文件，并写一份交付清单。**传输本身是手动步骤**（脚本只打印命令）。
#
# 为什么顺序不能换（判据真源：docs/research/01-delivery-and-deploy.md 的
# 「待实现脚本必须保留的方法性结论」段）：
#   自检必须在生成 `.sha256` 之前跑——自检不通过的 tar 绝不能拿到旁路文件，
#   否则它会带着一份「看似已校验」的旁路文件被传上服务器。
#
# 交付物（默认落在 dist/，该目录已在 .gitignore 里，不进版本库）：
#   vs-email-forwarding-<版本>.tar  + .tar.sha256   每次发版都重打
#   postgres-17.tar                + .tar.sha256   一次性介质；--app-only 不动它
#   delivery-manifest.txt                          交付清单（tag / image id / 基础镜像 digest / 各 tar 的 sha256）
#
# 为什么要带 postgres:17：服务器不能直连内网 Docker 源（路径式暴露、非 registry 协议），
# 运行期 compose 里的 `postgres:17` 必须由 tar 带进去；而 python / uv 两个构建期基础镜像
# **不再需要带入**——构建不发生在服务器（见 deploy/build.sh）。
#
# 用法：
#     bash deploy/package.sh                 # 首次交付：应用镜像 + postgres:17 两件都打
#     bash deploy/package.sh --app-only      # 日常发版：只重打应用镜像
#     bash deploy/package.sh --tag vs-email-forwarding:0.1.1
#     bash deploy/package.sh --out-dir dist
#     bash deploy/package.sh --print-steps   # 只打印将执行的步骤，不执行
#     bash deploy/package.sh --help
#
# 环境变量：
#     POSTGRES_REF   运行期数据库镜像（默认 postgres:17）
#     OUT_DIR        产出目录（默认：仓库根的 dist/）
#
# 退出码：0 = 每件交付物都已自检通过、旁路文件已生成并自校验；1 = 任一步失败。
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

DOCKERFILE="${REPO_ROOT}/Dockerfile"
PYPROJECT="${REPO_ROOT}/pyproject.toml"
IMAGE_NAME="vs-email-forwarding"
POSTGRES_REF="${POSTGRES_REF:-postgres:17}"
OUT_DIR="${OUT_DIR:-${REPO_ROOT}/dist}"

# 每个 tar 恰好含一个镜像，所以自检的期望条目数固定为 1；件数由本脚本控制，
# 而不是把「本次要带几个」做成一个可以被传错、又看不出错在哪的入参。
EXPECTED_IMAGES_PER_ARCHIVE=1

TAG=""
APP_ONLY=0
PRINT_ONLY=0

info() { echo "[信息] $*"; }
warn() { echo "[警告] $*" >&2; }
fail() { echo "[失败] $*" >&2; exit 1; }

usage() {
    cat <<'EOF'
用法：bash deploy/package.sh [选项]

产出交付物到 dist/：对每个镜像依次做「docker save → 归档自检 → 生成 sha256 旁路文件」，
最后写一份 delivery-manifest.txt。传输、load 与载入后复检仍是手动步骤（脚本会打印）。

选项：
  -h, --help        显示本说明
      --tag TAG     应用镜像 tag（默认：vs-email-forwarding:<pyproject.toml 的 version>）
      --app-only    只打包应用镜像（日常发版；不重打 postgres 那份一次性介质）
      --out-dir D   产出目录（默认：仓库根的 dist/）
      --print-steps 只打印将要执行的步骤，不执行

环境变量：
  POSTGRES_REF      运行期数据库镜像（默认 postgres:17）
  OUT_DIR           同 --out-dir

退出码：
  0 = 全部交付物自检通过、旁路文件已生成并自校验
  1 = 前置条件不满足，或任一步失败（自检不通过的 tar 会被删除，且不生成旁路文件）
EOF
}

read_project_version() {
    python3 - "${PYPROJECT}" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as handle:
    print(tomllib.load(handle)["project"]["version"])
PY
}

# 镜像引用 → ASCII 文件名主干：`repo:tag` 的 `/` 与 `:` 一律换成 `-`。
# 文件名必须 ASCII（介质传输渠道的硬约束，见 research 01 的方法性结论段）。
ref_to_basename() {
    printf '%s' "$1" | sed 's|[/:]|-|g'
}

# 打一件交付物：导出 → 自检 → 生成旁路文件 → 自校验。任一步失败即整体失败。
package_one() {
    local ref="$1"
    local base="$2"
    local tar_path="${OUT_DIR}/${base}.tar"

    docker image inspect "${ref}" >/dev/null 2>&1 \
        || fail "本机没有镜像 ${ref}：应用镜像请先跑 bash deploy/build.sh；运行期镜像请先 docker pull ${ref}。"

    info "【${ref}】① 导出：docker save -o ${tar_path} ${ref}"
    info "         （save / load 一律不传 --platform：服务器 API 1.45 不支持该参数）"
    # 清掉同名旧产物：留着旧 tar 或旧旁路文件，正是「传错版本」的来源。
    rm -f "${tar_path}" "${tar_path}.sha256"
    docker save -o "${tar_path}" "${ref}" || fail "docker save 失败：${ref}"
    [ -s "${tar_path}" ] || fail "docker save 产出的 ${tar_path} 是空文件。"

    info "【${ref}】② 归档自检：python3 deploy/check_archive.py ${base}.tar --expected-images ${EXPECTED_IMAGES_PER_ARCHIVE}"
    if ! python3 "${SCRIPT_DIR}/check_archive.py" "${tar_path}" --expected-images "${EXPECTED_IMAGES_PER_ARCHIVE}"; then
        rm -f "${tar_path}"
        fail "归档自检未通过，已删除 ${tar_path}：自检不通过的 tar 绝不能生成 .sha256 旁路文件，更不能传上服务器。"
    fi

    info "【${ref}】③ 生成 sha256 旁路文件（格式：<hash>␠␠<name>，两个空格 + LF）"
    # 先 cd 到产出目录再传 basename——旁路文件里必须是**文件名**而不是路径，
    # 否则在服务器上 `sha256sum -c` 会按产出机上的绝对路径去找文件。
    (cd "${OUT_DIR}" && sha256sum "${base}.tar" > "${base}.tar.sha256") \
        || fail "生成 ${base}.tar.sha256 失败。"
    (cd "${OUT_DIR}" && sha256sum -c --status "${base}.tar.sha256") \
        || fail "${base}.tar.sha256 自校验失败。"

    info "【${ref}】✅ ${base}.tar  $(cut -d' ' -f1 "${tar_path}.sha256")"
}

# 交付清单：把「这一批 tar 是从哪些镜像、哪些基础层做出来的」写成一份可随介质一起走的记录。
# digest 是**交付时实测并写下**的读数，不是脚本里冻死的常量（该冻死的只有 Dockerfile 的 FROM）。
write_manifest() {
    local manifest="${OUT_DIR}/delivery-manifest.txt"
    local generated_at engine git_rev name sha size tag_platform tag_id

    generated_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    engine="$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo '未知')"
    git_rev="$(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || echo '未知')"
    tag_id="$(docker image inspect "${TAG}" --format '{{.Id}}')"
    tag_platform="$(docker image inspect "${TAG}" --format '{{.Os}}/{{.Architecture}}')"

    {
        echo "# vs-email-forwarding 交付清单"
        echo "# 本文件由 deploy/package.sh 生成；digest 与 sha256 都是生成时的实测读数。"
        echo
        echo "生成时间(UTC)：${generated_at}"
        echo "git 提交：${git_rev}"
        echo "Docker Engine：${engine}"
        echo
        echo "应用镜像：${TAG}"
        echo "  image id ：${tag_id}"
        echo "  平台     ：${tag_platform}"
        echo
        echo "构建期基础镜像（已钉在 Dockerfile 的 FROM ARG 里）："
        grep -E '^ARG (PYTHON_IMAGE|UV_IMAGE)=' "${DOCKERFILE}" | sed 's/^/  /'
        echo
        echo "运行期数据库镜像：${POSTGRES_REF}"
        echo "  digest   ：$(docker image inspect "${POSTGRES_REF}" --format '{{join .RepoDigests " "}}' 2>/dev/null || echo '未知')"
        echo
        echo "交付物（每件都必须与它的 .sha256 旁路文件一起传）："
        for name in "${OUT_DIR}"/*.tar; do
            [ -f "${name}" ] || continue
            sha="$(cut -d' ' -f1 "${name}.sha256" 2>/dev/null || echo '缺少旁路文件')"
            size="$(stat -c %s "${name}")"
            echo "  $(basename "${name}")"
            echo "    sha256：${sha}"
            echo "    字节  ：${size}"
        done
        echo
        echo "带入服务器（手动，四步；判据出处：docs/research/01-delivery-and-deploy.md）："
        echo "  1) 传输（可续传、不得改动二进制内容、文件名 ASCII、.env 单独传）："
        echo "     rsync -av --partial $(basename "${OUT_DIR}")/*.tar $(basename "${OUT_DIR}")/*.sha256 <用户>@<服务器>:<交付目录>/"
        echo "  2) 校验完整性：cd <交付目录> && sha256sum -c --status <tar>.sha256"
        echo "  3) 载入：docker load -i <tar>              # 不带 --platform"
        echo "  4) 载入后复检架构：docker image inspect --format '{{.Os}}/{{.Architecture}}' <tag>"
        echo "     任一 tag 不是 linux/amd64 即失败，不要继续往下走。"
        echo
        echo "载入后起服务：bash deploy/migrate.sh && bash deploy/run.sh"
        echo "回滚：改 .env 的 VS_EMAIL_FORWARDING_IMAGE 成上一个版本 tag 再 docker compose up -d；"
        echo "      含破坏性迁移时另跑一次 alembic downgrade（刻意不脚本化，见 research 01「维护与回滚」段）。"
    } > "${manifest}"
    info "交付清单：${manifest}"
}

# 解析参数：只认这几条，其余一律拒绝。
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
        --app-only)
            APP_ONLY=1
            shift
            ;;
        --out-dir)
            [ "$#" -ge 2 ] || fail "--out-dir 需要一个参数（产出目录）。"
            OUT_DIR="$2"
            shift 2
            ;;
        --print-steps)
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
command -v docker >/dev/null 2>&1 || fail "本机没有 docker，无法导出镜像。"

if [ -z "${TAG}" ]; then
    version="$(read_project_version)" || fail "读不出 ${PYPROJECT} 的 [project] version（需要 python3 的 tomllib）。"
    [ -n "${version}" ] || fail "${PYPROJECT} 的 [project] version 是空的。"
    TAG="${IMAGE_NAME}:${version}"
fi

APP_BASE="$(ref_to_basename "${TAG}")"
POSTGRES_BASE="$(ref_to_basename "${POSTGRES_REF}")"

info "仓库根  ：${REPO_ROOT}"
info "产出目录：${OUT_DIR}"
info "应用镜像：${TAG}"
if [ "${APP_ONLY}" -eq 1 ]; then
    info "本次只打应用镜像（--app-only）；postgres 那份一次性介质不动。"
else
    info "运行期镜像：${POSTGRES_REF}"
fi

if [ "${PRINT_ONLY}" -eq 1 ]; then
    info "将按此顺序执行（每件都是：save → 自检 → 生成并自校验 sha256）："
    info "  1) docker save -o ${OUT_DIR}/${APP_BASE}.tar ${TAG}"
    info "  2) python3 deploy/check_archive.py ${APP_BASE}.tar --expected-images ${EXPECTED_IMAGES_PER_ARCHIVE}"
    info "  3) sha256sum ${APP_BASE}.tar > ${APP_BASE}.tar.sha256（并立即 -c 自校验）"
    if [ "${APP_ONLY}" -eq 0 ]; then
        info "  4) docker save -o ${OUT_DIR}/${POSTGRES_BASE}.tar ${POSTGRES_REF}"
        info "  5) python3 deploy/check_archive.py ${POSTGRES_BASE}.tar --expected-images ${EXPECTED_IMAGES_PER_ARCHIVE}"
        info "  6) sha256sum ${POSTGRES_BASE}.tar > ${POSTGRES_BASE}.tar.sha256（并立即 -c 自校验）"
    fi
    info "  7) 写 ${OUT_DIR}/delivery-manifest.txt"
    exit 0
fi

mkdir -p "${OUT_DIR}"
[ -w "${OUT_DIR}" ] || fail "产出目录不可写：${OUT_DIR}"

package_one "${TAG}" "${APP_BASE}"

if [ "${APP_ONLY}" -eq 1 ]; then
    if [ ! -f "${OUT_DIR}/${POSTGRES_BASE}.tar" ]; then
        warn "本次没打 postgres 那份，而 ${OUT_DIR}/${POSTGRES_BASE}.tar 也不存在：首次交付必须把运行期镜像一起带上（去掉 --app-only 重跑）。"
    fi
else
    package_one "${POSTGRES_REF}" "${POSTGRES_BASE}"
fi

write_manifest

info ""
info "交付物已就绪（传输仍是手动步骤）："
for name in "${OUT_DIR}"/*.tar; do
    [ -f "${name}" ] || continue
    info "  $(basename "${name}")  +  $(basename "${name}").sha256"
done
