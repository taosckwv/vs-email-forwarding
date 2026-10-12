#!/usr/bin/env bash
# =============================================================================
# vs-email-forwarding 一键门禁
#
# 按固定顺序执行 5 步，任何一步失败立即以非零退出码结束，后续步骤不再执行。
#
#   1) uv run ruff format --check .   代码格式检查
#   2) uv run ruff check .            静态检查（lint）
#   3) uv run mypy src                类型检查
#   4) uv run pytest --cov ...        单元与集成测试 + 覆盖率（覆盖率只作诊断，不作门槛）
#   5) uv run mutmut run              变异测试（证明断言真的能失败）
#
# 第 4、5 步的判据见 docs/research/03-dependencies-and-toolchain.md 的「测试质量的度量口径」；
# 测试侧规则（哪些该写、怎么写、评审检查表）见 docs/testing.md。
# 第 5 步的范围固定为 tests/unit（见 pyproject.toml 的 [tool.mutmut]）：
# 门禁结论不得随容器是否启动而改变。
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
run_step "[1/5] 代码格式检查（ruff format --check）"  ruff format --check .
run_step "[2/5] 静态检查（ruff check）"               ruff check .
run_step "[3/5] 类型检查（mypy src）"                  mypy src
run_step "[4/5] 单元与集成测试 + 覆盖率（pytest --cov）" pytest --cov --cov-report=term-missing

# --- 第 5 步：变异测试 -------------------------------------------------------
# 判据是**结构性红线，不是百分比阈值**（依据见 docs/research/03-...md）：
#   ① mutmut 本身运行失败；② 变异体总数为 0（配置失效，这一步等于没跑）；
#   ③ 被杀数为 0 且存在存活变异（断言与实现完全同义反复）。
# 其余情况一律通过，只把存活清单打出来供人工评审。
echo
echo "==> [5/5] 变异测试（mutmut：证明断言真的能失败）"
if ! uv run mutmut run; then
    echo
    echo "[失败] [5/5] 变异测试（mutmut 运行失败）" >&2
    echo "门禁未通过：本项目没有 CI 兜底，修完请重跑 scripts/check.sh。" >&2
    exit 1
fi

if ! uv run mutmut export-cicd-stats >/dev/null 2>&1; then
    echo
    echo "[失败] [5/5] 无法生成变异统计（mutmut export-cicd-stats 失败）" >&2
    exit 1
fi

if ! uv run python - <<'PY'
import json
import pathlib
import sys

stats_path = pathlib.Path("mutants/mutmut-cicd-stats.json")
if not stats_path.is_file():
    print("[失败] 找不到 mutants/mutmut-cicd-stats.json：变异统计未生成。", file=sys.stderr)
    raise SystemExit(1)

stats = json.loads(stats_path.read_text(encoding="utf-8"))
killed, survived, total = stats["killed"], stats["survived"], stats["total"]
print(f"变异体：{total} 个，杀死 {killed} 个，存活 {survived} 个")

if total == 0:
    print("[失败] 变异体总数为 0：mutmut 配置失效，这一步等于没跑。", file=sys.stderr)
    raise SystemExit(1)

if killed == 0 and survived > 0:
    print(
        "[失败] 变异体全部存活：断言与实现完全同义反复——把实现改错，测试也不会红。\n"
        "        先补能失败的断言，不要用 `# pragma: no mutate` 把存活抹掉。",
        file=sys.stderr,
    )
    raise SystemExit(1)

if survived:
    print(
        f"[评审项] {survived} 个变异体存活——门禁不设分数阈值，"
        "请逐条判断是真漏测还是等价变异：\n"
        "         清单：uv run mutmut results\n"
        "         明细：uv run mutmut browse"
    )
PY
then
    echo
    echo "[失败] [5/5] 变异测试判据未通过（原因见上方）" >&2
    echo "门禁未通过：本项目没有 CI 兜底，修完请重跑 scripts/check.sh。" >&2
    exit 1
fi
echo "[通过] [5/5] 变异测试（mutmut）"

echo
echo "门禁全部通过，可以提交。"
exit 0
