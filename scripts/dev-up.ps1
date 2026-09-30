#Requires -Version 5.1
<#
.SYNOPSIS
    vs-email-forwarding 开发依赖一键启动（Windows / PowerShell 版）。

.DESCRIPTION
    用 compose.dev.yaml 起开发用基础设施：PostgreSQL 17 + Mailpit（SMTP/IMAP/Web UI）。
    应用本体不在容器里跑，用 uv 在宿主机跑：
        uv run uvicorn vs_email_forwarding.main:app --reload

.EXAMPLE
    .\scripts\dev-up.ps1

.NOTES
    前置条件：**已安装 Docker Desktop**。本机目前尚未安装（见 docs/open-questions.md D3），
    没装的话本脚本只会打印安装提示并退出，不会做任何事。
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

# 若宿主把 PS7.4+ 的 PSNativeCommandUseErrorActionPreference 设为 $true，
# 原生命令的非零退出码会抛 terminating error，破坏下面基于 $LASTEXITCODE 的判断。
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot

# --- 前置检查：Docker ---------------------------------------------------------
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host '[阻塞] 找不到 docker 命令。' -ForegroundColor Red
    Write-Host '本机需要先安装 Docker Desktop（Windows 需要 WSL2，安装过程可能需要管理员权限并重启）：' -ForegroundColor Yellow
    Write-Host '  https://docs.docker.com/desktop/setup/install/windows-install/' -ForegroundColor Yellow
    Write-Host '装好后启动 Docker Desktop，等托盘图标变为运行中，再重跑本脚本。' -ForegroundColor Yellow
    exit 127
}

# 守护进程可能没起来（Docker Desktop 未启动）
$daemonOk = $true
try {
    & docker info *> $null
    if ($LASTEXITCODE -ne 0) { $daemonOk = $false }
} catch {
    $daemonOk = $false
}
if (-not $daemonOk) {
    Write-Host '[阻塞] docker 命令存在，但连不上 Docker 守护进程。' -ForegroundColor Red
    Write-Host '请先启动 Docker Desktop（等托盘图标变为运行中）再重跑本脚本。' -ForegroundColor Yellow
    exit 1
}

# --- 起依赖 -------------------------------------------------------------------
Write-Host '==> 启动开发依赖（PostgreSQL + Mailpit）' -ForegroundColor Cyan
& docker compose -f compose.dev.yaml up -d
if ($LASTEXITCODE -ne 0) {
    Write-Host '[失败] docker compose 启动失败，请检查上面的输出。' -ForegroundColor Red
    exit $LASTEXITCODE
}

# --- 打印连接信息（与 compose.dev.yaml 保持一致） ------------------------------
Write-Host ''
Write-Host '开发依赖已启动：' -ForegroundColor Green
Write-Host '  Mailpit Web UI : http://127.0.0.1:8025   （看所有收下的邮件，也可手动回信）'
Write-Host '  SMTP           : 127.0.0.1:1025          （应用发信走这里）'
Write-Host '  IMAP           : 127.0.0.1:1143          （应用拉取入站回复走这里）'
Write-Host '  PostgreSQL     : 127.0.0.1:5432'
Write-Host '    开发库       : vs_email_forwarding_dev  (用户 vs_email_forwarding / 口令 vs_email_forwarding_dev)'
Write-Host '    测试库       : vs_email_forwarding_test （需手工创建，命令见 compose.dev.yaml 末尾说明）'
Write-Host ''
Write-Host 'Mailpit 的用途：验证「发出去 -> 被拉回来 -> 关联上原邮件」的完整回路，' -ForegroundColor Cyan
Write-Host '不需要真实邮箱账号，也不会把测试邮件发到外网。' -ForegroundColor Cyan
Write-Host ''
Write-Host '接下来（应用本体在宿主机跑，不在容器里）：'
Write-Host '  uv sync'
Write-Host '  uv run uvicorn vs_email_forwarding.main:app --reload'
Write-Host ''
Write-Host '停止依赖：docker compose -f compose.dev.yaml down'
Write-Host '连数据一起清掉：docker compose -f compose.dev.yaml down -v'
exit 0
