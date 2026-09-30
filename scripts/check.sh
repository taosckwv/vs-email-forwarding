#!/usr/bin/env bash
# =============================================================================
# vs-email-forwarding 一键门禁（Linux / macOS 版，CentOS 8 可用）
#
# 与 scripts/check.ps1 **行为完全一致**：按固定顺序执行 4 步，任何一步失败立即
# 以非零退出码结束，后续步骤不再执行。
#
#   1) uv run ruff format --check .   代码格式检查
#   2) uv run ruff check .            静态检查（lint）
#   3) uv run mypy src                类型检查
#   4) uv run pytest                  单元与集成测试
#
# 本项目**不跑 CI**（见 AGENTS.md §9），门禁只能靠自己跑：
# 提交前必须执行本脚本，必须全绿。
#
# 用法（可以从任意目录调用，脚本自己推导仓库根）：
#   bash scripts/check.sh
#   ./scripts/check.sh
#
# 前置条件：已安装 uv（`uv --version` 可用）。
# 说明：`set -e` 负责"任何命令失败就退出"；每一步另外用 if 显式判断，
#       这样才能打印出"是哪一步失败"的中文提示（`if` 条件里的命令不触发 errexit）。
# =============================================================================
set -euo pipefail

# 仓库根 = 本脚本所在目录（scripts/）的上一级；
# 用 BASH_SOURCE 推导，保证从任意工作目录调用都作用于本仓库。
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

if ! command -v uv >/dev/null 2>&1; then
    echo "[致命] 找不到 uv 命令：请先安装 uv 后再跑门禁（见 AGENTS.md §3）。" >&2
    exit 127
fi

# 单步执行器：$1 = 中文标签，其余 = 传给 uv run 的参数
run_step() {
    local label="$1"
    shift
    echo
    echo "==> ${label}"
    if uv run "$@"; then
        echo "[通过] ${label}"
    else
        local code=$?
        echo
        echo "[失败] ${label}（退出码 ${code}）" >&2
        echo "门禁未通过：本项目没有 CI 兜底，修完请重跑 scripts/check.sh。" >&2
        exit "${code}"
    fi
}

# 顺序即约定顺序，不要随意调整
run_step "[1/4] 代码格式检查（ruff format --check）" ruff format --check .
run_step "[2/4] 静态检查（ruff check）"              ruff check .
run_step "[3/4] 类型检查（mypy src）"                 mypy src
run_step "[4/4] 单元与集成测试（pytest）"             pytest

echo
echo "门禁全部通过，可以提交。"
exit 0
