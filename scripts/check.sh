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
#   5) uv run mutmut run              变异测试（证明断言真的能失败）+ 存活逐条复验
#
# 第 4、5 步的判据见 docs/research/03-dependencies-and-toolchain.md 的「测试质量的度量口径」；
# 测试侧规则（哪些该写、怎么写、评审检查表）见 docs/testing.md。
# 第 5 步的范围固定为 tests/unit（见 pyproject.toml 的 [tool.mutmut]）：
# 门禁结论不得随容器是否启动而改变。
# 第 5 步的存活复验同样只跑 tests/unit，且**不采信 mutmut 自己写的存活判定**：
# mutmut 只按 tests_by_mangled_function_name 给每个变异体挑 1~2 条用例跑，
# 会把其实会被全套单测杀死的变异体记成存活（度量缺陷，见上述主题册）。
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

# --- 第 5 步：变异测试 + 存活逐条复验 ----------------------------------------
# 判据是**结构性红线，不是百分比阈值**（依据见 docs/research/03-...md）：
#   ① mutmut 本身运行失败；② 变异体总数为 0（配置失效，这一步等于没跑）；
#   ③ 被杀数为 0 且存在存活变异（断言与实现完全同义反复）。
# 本步另加两条同性质的红线：
#   ④ mutmut 记下的退出码里出现「既不是杀死也不是存活」的值（尤其 2——pytest 的
#      收集/导入错误，mutmut 的 status_by_exit_code 却把它当"check was interrupted
#      by user"，等于静默放过）；
#   ⑤ 复验出现无法解释的退出码，或复验环境自检不通过。
# 其余情况一律通过，只把**复验后**的真实存活清单打出来供人工评审。
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
import glob
import json
import os
import pathlib
import subprocess
import sys

MUTANTS_DIR = pathlib.Path("mutants")
MUTANTS_ABS = MUTANTS_DIR.resolve()

# 复验进程必须 import mutants/ 里的变异源码，而不是仓库 src/（与 mutmut 内部的
# setup_source_paths 一致）：PYTHONPATH 先于 .venv 里指向 src/ 的 editable 路径。
REVERIFY_PYTHONPATH = os.pathsep.join([str(MUTANTS_ABS), str(MUTANTS_ABS / "src")])
REVERIFY_ARGS = [
    "-m",
    "pytest",
    "tests/unit",  # 只跑全套单测：门禁结论不得随容器是否启动而改变
    "-q",
    "-p",
    "no:randomly",
    # 收集/导入错误按"测试失败"计（退出码 1）。缺了它 pytest 会退回退出码 2，
    # 而 mutmut 把 2 当"用户中断"——这正是存活误报的根因。
    "--continue-on-collection-errors",
    "--rootdir=.",
]
REVERIFY_TIMEOUT_S = 300

# mutmut 的退出码口径（mutmut/stats.py 的 status_by_exit_code）：1 = 测试失败；
# 3 = pytest 内部错误（mutmut 同样算杀死）。其余非 0 值一律不认。
KILLED_EXIT_CODES = (1, 3)
# 变异体把测试套件本身弄到加载不起来（pytest 的 4：conftest 导入失败）时，套件同样
# 报了错——判为已检出，但单独列出：它不是断言失败，排查时要知道区别。
LOAD_ERROR_EXIT_CODES = (4,)


def fail(*lines: str) -> None:
    for line in lines:
        print(line, file=sys.stderr)
    print("门禁未通过：本项目没有 CI 兜底，修完请重跑 scripts/check.sh。", file=sys.stderr)
    raise SystemExit(1)


def reverify(mutant_name: str | None) -> subprocess.CompletedProcess:
    """用全套 tests/unit 复验一次，返回 pytest 的退出码与输出。

    ``mutant_name=None`` 表示不设变异体（对照组：全套单测必须通过）。
    """
    env = dict(os.environ, PYTHONPATH=REVERIFY_PYTHONPATH)
    env.pop("MUTANT_UNDER_TEST", None)
    if mutant_name is not None:
        env["MUTANT_UNDER_TEST"] = mutant_name
    try:
        return subprocess.run(
            [sys.executable, *REVERIFY_ARGS],
            cwd=MUTANTS_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=REVERIFY_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        fail(f"[失败] 复验超时（超过 {REVERIFY_TIMEOUT_S}s）：{mutant_name}")
        raise  # 上面的 fail 已经退出，这里只让类型检查安心


# --- ①②③ 原有的三条结构性红线（一条都没有放宽） ---------------------------
stats_path = MUTANTS_DIR / "mutmut-cicd-stats.json"
if not stats_path.is_file():
    fail("[失败] 找不到 mutants/mutmut-cicd-stats.json：变异统计未生成。")

stats = json.loads(stats_path.read_text(encoding="utf-8"))
killed, survived, total = stats["killed"], stats["survived"], stats["total"]
print(f"变异体：{total} 个，杀死 {killed} 个，存活 {survived} 个")

if total == 0:
    fail("[失败] 变异体总数为 0：mutmut 配置失效，这一步等于没跑。")

if killed == 0 and survived > 0:
    fail(
        "[失败] 变异体全部存活：断言与实现完全同义反复——把实现改错，测试也不会红。",
        "        先补能失败的断言，不要用 `# pragma: no mutate` 把存活抹掉。",
    )

# --- ④ 变异体清单的唯一来源：mutmut 自己写的 exit_code_by_key ---------------
exit_code_by_key: dict = {}
for meta_path in sorted(glob.glob("mutants/**/*.meta", recursive=True)):
    data = json.loads(pathlib.Path(meta_path).read_text(encoding="utf-8"))
    exit_code_by_key.update(data.get("exit_code_by_key", {}))

if not exit_code_by_key:
    fail("[失败] mutants/**/*.meta 里没有任何变异体：清单为空，这一步等于没跑。")

killed_names: list[str] = []
survived_candidates: list[str] = []
unrecognized: list[tuple[str, object]] = []
for name, code in sorted(exit_code_by_key.items()):
    if code in KILLED_EXIT_CODES:
        killed_names.append(name)
    elif code == 0:
        survived_candidates.append(name)
    else:
        unrecognized.append((name, code))

if unrecognized:
    lines = [f"[失败] mutmut 记录的退出码里有 {len(unrecognized)} 条既不是「杀死」也不是「存活」："]
    for name, code in unrecognized:
        note = ""
        if code == 2:
            note = (
                "  ← pytest 的收集/导入错误；mutmut 的 status_by_exit_code 把 2 当成"
                "「check was interrupted by user」，等于静默放过"
            )
        lines.append(f"        {name}（退出码 {code}）{note}")
    lines.append("        处置：删掉 mutants/ 重新全量跑；仍出现就按缺陷排查，不得当成存活放过。")
    fail(*lines)

print(f"变异体清单（mutants/**/*.meta）：{len(exit_code_by_key)} 条")
if len(exit_code_by_key) != total:
    print(f"[提示] .meta 的条数与 mutmut 统计（{total}）不一致，以 .meta 清单为准。")

# --- 复验环境自检（两条对照，任何一条不成立就说明复验不可信） ----------------
# ① 对照组：不设变异体时全套 tests/unit 必须通过——否则是命令/环境本身坏了，
#    这时所有变异体都会被"检出"，门禁会假绿。
# ② 变异开关：MUTANT_UNDER_TEST=fail 是 mutmut 自带的开关，只要变异源码被真正加载，
#    被变异的函数一被调用就抛异常。若复验进程实际 import 的是仓库 src/，全套单测
#    仍然会通过，复验就变成一组假数字。
control = reverify(None)
if control.returncode != 0:
    fail(
        "[失败] 存活复验环境自检未通过：不设变异体时全套 tests/unit 没有通过（退出码 "
        f"{control.returncode}）。",
        "        复验命令或环境本身有问题，此时任何「检出」都不能采信。",
    )
print("复验环境自检：不设变异体时全套 tests/unit 通过（对照组）")

probe = reverify("fail")
if probe.returncode == 0:
    fail(
        "[失败] 存活复验环境自检未通过：MUTANT_UNDER_TEST=fail 下全套单测仍然通过，",
        "        说明复验进程没有加载 mutants/ 里的变异源码，复验结论不可信。",
    )
print(f"复验环境自检：MUTANT_UNDER_TEST=fail 的退出码为 {probe.returncode}（非 0，变异源码确实被加载）")

# --- ⑤ 逐条复验存活候选：只复验存活清单，不做全量复跑 -----------------------
real_killed = len(killed_names)
real_survived: list[str] = []
reclassified: list[str] = []
load_error_detected: list[tuple[str, int]] = []
broken: list[tuple[str, int, subprocess.CompletedProcess]] = []

for index, name in enumerate(survived_candidates, start=1):
    print(f"    复验 {index}/{len(survived_candidates)}：{name}")
    result = reverify(name)
    if result.returncode == 0:
        real_survived.append(name)
    elif result.returncode in KILLED_EXIT_CODES:
        reclassified.append(name)
        real_killed += 1
    elif result.returncode in LOAD_ERROR_EXIT_CODES:
        # 变异体让套件连加载都做不到（conftest 导入失败）：已检出，只是不是断言失败。
        load_error_detected.append((name, result.returncode))
        real_killed += 1
    else:
        broken.append((name, result.returncode, result))

if broken:
    lines = [f"[失败] 复验出现无法解释的退出码：{len(broken)} 条"]
    for name, code, result in broken:
        lines.append(f"        {name}（退出码 {code}）")
        if code == 2:
            lines.append(
                "          ← 仍是收集/导入错误。本轮已带 --continue-on-collection-errors，"
                "再出现 2 说明是环境问题，不是变异体的问题",
            )
        tail = (result.stdout or "").strip().splitlines()[-8:]
        tail += (result.stderr or "").strip().splitlines()[-8:]
        lines.extend(f"          | {line}" for line in tail)
    lines.append("        处置：先确认复验命令本身能跑通（容器未起也应能跑），再排查环境；不得当成存活放过。")
    fail(*lines)

print()
print(f"mutmut 原始判定：杀死 {killed} 个 / 存活 {survived} 个 / 共 {total} 个")
print(f"复验后真实判定：杀死 {real_killed} 个 / 存活 {len(real_survived)} 个（复验了 {len(survived_candidates)} 条存活候选）")

if reclassified:
    print()
    print(
        f"复验改判：{len(reclassified)} 条被 mutmut 记为「存活」的变异体，在全套 tests/unit 下会被杀死"
        "——这是 mutmut 只挑 1~2 条用例跑造成的误报：",
    )
    for name in reclassified:
        print(f"        {name}")

if load_error_detected:
    print()
    print(
        f"复验检出（非断言失败）：{len(load_error_detected)} 条变异体让测试套件连加载都做不到"
        "（退出码 "
        + "/".join(sorted({str(code) for _, code in load_error_detected}))
        + "，conftest 导入期就崩）——同样算杀死，但排查时按导入错误看：",
    )
    for name, code in load_error_detected:
        print(f"        {name}（退出码 {code}）")

if real_survived:
    print()
    print(f"[评审项] 复验后 {len(real_survived)} 个变异体真实存活——门禁不设分数阈值，")
    print("         请逐条判断是真漏测还是等价变异：")
    for name in real_survived:
        print(f"        {name}")
    print("         清单：uv run mutmut results")
    print("         明细：uv run mutmut browse")
PY
then
    echo
    echo "[失败] [5/5] 变异测试判据未通过（原因见上方）" >&2
    echo "门禁未通过：本项目没有 CI 兜底，修完请重跑 scripts/check.sh。" >&2
    exit 1
fi
echo "[通过] [5/5] 变异测试（mutmut + 存活复验）"

echo
echo "门禁全部通过，可以提交。"
exit 0
