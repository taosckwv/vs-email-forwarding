#Requires -Version 5.1
<#
.SYNOPSIS
    在**开发机**（Windows）上构建 mail-relay 应用镜像。

.DESCRIPTION
    流程：读版本号 -> docker build（linux/amd64）-> 打版本 tag 与 latest tag
          -> docker image inspect 打印镜像大小。

    离线交付的完整链路：
        deploy/build-image.ps1   （本脚本，开发机，需要有网）
        deploy/save-image.ps1    （开发机 -> dist/*.tar + sha256）
        deploy/load-image.sh     （服务器，docker load + 校验 sha256）
        deploy/migrate.sh        （服务器，Alembic 迁移）
        deploy/run.sh            （服务器，docker compose up -d + 健康检查）

.PARAMETER Version
    镜像版本号。不传时从仓库根的 pyproject.toml 的 [project] version 读取；
    读不到就退回 0.0.0-dev 并给出警告（骨架阶段 pyproject 可能还没就位）。

.PARAMETER ImageName
    镜像仓库名，默认 mail-relay。最终 tag 形如 mail-relay:1.2.3 与 mail-relay:latest。

.PARAMETER Platform
    目标架构，默认 linux/amd64。**不要随便改**：生产宿主是 CentOS 8 x86_64，
    与 save/load 的 --platform、compose 的 platform 三处必须一致。

.PARAMETER NoCache
    加 --no-cache 全量重建（排查"缓存里藏着旧依赖"这类问题时用）。

.EXAMPLE
    .\deploy\build-image.ps1
    .\deploy\build-image.ps1 -Version 0.2.0
    .\deploy\build-image.ps1 -NoCache

.NOTES
    前置条件：已安装 Docker Desktop（见 docs/open-questions.md D3），
    并且构建机可以访问 Docker Hub 与 ghcr.io（只有开发机需要联网）。
#>
[CmdletBinding()]
param(
    [string]$Version,
    [string]$ImageName = 'mail-relay',
    [string]$Platform = 'linux/amd64',
    [switch]$NoCache
)

$ErrorActionPreference = 'Stop'
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

# 仓库根 = 本脚本所在目录（deploy/）的上一级
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot

# --- 前置检查 ----------------------------------------------------------------
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host '[阻塞] 找不到 docker 命令。请先安装并启动 Docker Desktop 再构建镜像。' -ForegroundColor Red
    Write-Host '  https://docs.docker.com/desktop/setup/install/windows-install/' -ForegroundColor Yellow
    exit 127
}

$daemonOk = $true
try {
    & docker info *> $null
    if ($LASTEXITCODE -ne 0) { $daemonOk = $false }
} catch { $daemonOk = $false }
if (-not $daemonOk) {
    Write-Host '[阻塞] 连不上 Docker 守护进程，请先启动 Docker Desktop。' -ForegroundColor Red
    exit 1
}

# 记录构建机引擎版本：save 自 Engine v25 起产出 OCI 归档，--platform 需要 Engine v28+。
# 这里只打印，便于事后对账（正式核对在服务器侧，见 deploy/load-image.sh）。
$engineVersion = (& docker version --format '{{.Server.Version}}') 2>$null
Write-Host ("构建机 Docker Engine 版本：" + $engineVersion)

# --- 解析版本号 ---------------------------------------------------------------
if (-not $Version) {
    $pyproject = Join-Path $repoRoot 'pyproject.toml'
    if (Test-Path -LiteralPath $pyproject) {
        # 只在 [project] 段里找第一个 version = "..."，避免误读 [tool.*] 下的版本号
        $inProject = $false
        foreach ($line in Get-Content -LiteralPath $pyproject -Encoding UTF8) {
            if ($line -match '^\s*\[(.+?)\]\s*$') {
                $inProject = ($Matches[1].Trim() -eq 'project')
                continue
            }
            if ($inProject -and $line -match '^\s*version\s*=\s*"([^"]+)"') {
                $Version = $Matches[1]
                break
            }
        }
    }
}
if (-not $Version) {
    $Version = '0.0.0-dev'
    Write-Host ('[警告] 没能从 pyproject.toml 读到版本号，退回 ' + $Version + '（骨架阶段的预期情况）。' +
                '正式交付请用 -Version 显式指定。') -ForegroundColor Yellow
}

$versionTag = "${ImageName}:${Version}"
$latestTag = "${ImageName}:latest"

# --- 构建 ---------------------------------------------------------------------
$buildArgs = @(
    'build',
    '--platform', $Platform,
    '--file', 'Dockerfile',
    '--tag', $versionTag,
    '--tag', $latestTag
)
if ($NoCache) { $buildArgs += '--no-cache' }
$buildArgs += '.'

Write-Host ''
Write-Host ("==> docker build（平台 " + $Platform + "，tag " + $versionTag + " 与 " + $latestTag + "）") -ForegroundColor Cyan
Write-Host ("    docker " + ($buildArgs -join ' '))

& docker @buildArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host '[失败] 镜像构建失败，请检查上面的构建输出。' -ForegroundColor Red
    exit $LASTEXITCODE
}

# --- 打印镜像信息与大小 -------------------------------------------------------
function Format-Size {
    param([long]$Bytes)
    if ($Bytes -ge 1GB) { return ('{0:N2} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N2} MB' -f ($Bytes / 1MB)) }
    return ('{0:N0} KB' -f ($Bytes / 1KB))
}

Write-Host ''
Write-Host '==> 构建结果' -ForegroundColor Cyan
foreach ($tag in @($versionTag, $latestTag)) {
    $rawSize = (& docker image inspect --format '{{.Size}}' $tag) 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $rawSize) {
        Write-Host ("  [警告] 无法读取镜像大小：" + $tag) -ForegroundColor Yellow
        continue
    }
    $bytes = [long]$rawSize
    Write-Host ("  " + $tag + "  大小 " + (Format-Size -Bytes $bytes) + "（" + $bytes + " 字节）")
}

Write-Host ''
Write-Host '下一步（生成离线交付包）：' -ForegroundColor Green
Write-Host ("  .\deploy\save-image.ps1 -Version " + $Version)
Write-Host '提示：latest 只是开发机上的便利 tag；离线服务器用 .env 里的 MAIL_RELAY_IMAGE' -ForegroundColor Cyan
Write-Host '      指定具体版本 tag（便于回滚），不要依赖 latest。' -ForegroundColor Cyan
exit 0
