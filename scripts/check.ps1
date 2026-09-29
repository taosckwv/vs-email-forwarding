#Requires -Version 5.1
<#
.SYNOPSIS
    mail-relay 一键门禁（Windows / PowerShell 版）。

.DESCRIPTION
    与 scripts/check.sh **行为完全一致**：按固定顺序执行 4 步，任何一步失败立即以
    非零退出码结束，后续步骤不再执行。

      1) uv run ruff format --check .   代码格式检查
      2) uv run ruff check .            静态检查（lint）
      3) uv run mypy src                类型检查
      4) uv run pytest                  单元与集成测试

    本项目**不跑 CI**（见 AGENTS.md §9），门禁只能靠自己跑：
    提交前必须执行本脚本，必须全绿。

.PARAMETER 无
    本脚本不接受参数，避免"本地跑的参数与别人不同"导致门禁不可比。

.EXAMPLE
    pwsh -File .\scripts\check.ps1
    .\scripts\check.ps1

.NOTES
    前置条件：已安装 uv（`uv --version` 可用）。可从任意目录调用——脚本用
    $PSScriptRoot 推导仓库根，再切到仓库根执行（ruff/mypy/pytest 都按仓库根解析配置）。
#>
[CmdletBinding()]
param()

# 任一步骤出错就停（下面用 $LASTEXITCODE 判断原生命令退出码）
$ErrorActionPreference = 'Stop'

# PowerShell 7.4+ 引入：若被宿主设为 $true，原生命令的非零退出码会抛 terminating error，
# 从而跳过我们自己的退出码处理逻辑。这里显式关掉，保证 PS5.1 与 PS7+ 行为一致。
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

# 仓库根 = 本脚本所在目录（scripts/）的上一级
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host '[致命] 找不到 uv 命令：请先安装 uv 后再跑门禁（见 AGENTS.md §3）。' -ForegroundColor Red
    exit 127
}

# 步骤表：顺序即约定顺序，不要随意调整
$steps = @(
    @{ Label = '[1/4] 代码格式检查（ruff format --check）'; Args = @('ruff', 'format', '--check', '.') },
    @{ Label = '[2/4] 静态检查（ruff check）';             Args = @('ruff', 'check', '.') },
    @{ Label = '[3/4] 类型检查（mypy src）';                Args = @('mypy', 'src') },
    @{ Label = '[4/4] 单元与集成测试（pytest）';            Args = @('pytest') }
)

foreach ($step in $steps) {
    Write-Host ''
    Write-Host ("==> " + $step.Label) -ForegroundColor Cyan

    # 先拼出完整参数再 splat（@uvArgs）：写成 `& uv run @($step.Args)` 会把数组当成
    # **一个**参数传下去（不展开），必须避免。
    $uvArgs = @('run') + [string[]]$step.Args
    & uv @uvArgs
    $exitCode = $LASTEXITCODE

    if ($exitCode -ne 0) {
        Write-Host ''
        Write-Host ("[失败] " + $step.Label + "（退出码 " + $exitCode + "）") -ForegroundColor Red
        Write-Host '门禁未通过：本项目没有 CI 兜底，修完请重跑 scripts/check.ps1。' -ForegroundColor Red
        exit $exitCode
    }

    Write-Host ("[通过] " + $step.Label) -ForegroundColor Green
}

Write-Host ''
Write-Host '门禁全部通过，可以提交。' -ForegroundColor Green
exit 0
