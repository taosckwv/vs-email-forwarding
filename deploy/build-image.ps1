#Requires -Version 5.1
<#
.SYNOPSIS
    在**开发机**（Windows）上构建 vs-email-forwarding 应用镜像。

.DESCRIPTION
    流程：读版本号 -> docker build（linux/amd64）-> 打版本 tag 与 latest tag
          -> docker image inspect 打印镜像大小。

    离线交付的完整链路：
        deploy/build-image.ps1   （本脚本，开发机，需要有网）—— 平台在这里钉死
        deploy/save-image.ps1    （开发机 -> dist/*.tar + 保存前架构校验 + 归档自检 + sha256）
        deploy/load-image.sh     （服务器，校验 sha256 -> docker load -> 载入后架构校验）
        deploy/migrate.sh        （服务器，Alembic 迁移）
        deploy/run.sh            （服务器，docker compose up -d + 健康检查）

    本脚本的 `docker build --platform` 是**交付链上唯一真正决定镜像架构的地方**：
    save / load 都**不带** `--platform`（服务器实测 Client / Server 均为 26.1.3、
    API 1.45，而该参数需要 v28+ / API 1.48），架构靠保存前与载入后的校验来保证。

.PARAMETER Version
    镜像版本号。不传时从仓库根的 pyproject.toml 的 [project] version 读取；
    读不到就退回 0.0.0-dev 并给出警告（预研阶段 pyproject 可能还没就位）。

.PARAMETER ImageName
    镜像仓库名，默认 vs-email-forwarding。最终 tag 形如 vs-email-forwarding:1.2.3 与 vs-email-forwarding:latest。

.PARAMETER Platform
    目标架构，默认 linux/amd64。**不要随便改**：生产宿主是 CentOS 8 x86_64。
    构建期是唯一钉死平台的地方（`docker build --platform`，BuildKit 语义）；
    compose 里的 platform 只是运行期保险，save / load 不再带 --platform
    （服务器 API 1.45 < 1.48），改为保存前 / 载入后校验架构。

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
    [string]$ImageName = 'vs-email-forwarding',
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

# 记录构建机引擎版本，便于事后对账（服务器版本已实测，见 deploy/README.md §3.0）。
# 关于 `docker build --platform` 的语义（BuildKit）：
#   * 它钉死的是**目标平台**：本文件不使用 TARGETPLATFORM / BUILDPLATFORM 参数化写法，
#     所以除本机架构无关的多阶段拷贝外，整个构建都按该平台进行（本机是 arm64 时走模拟）；
#   * 该参数需要 BuildKit（Docker Desktop 默认开启；Dockerfile 里也用了
#     `RUN --mount=type=cache`，本身就是 BuildKit 语法）。若把 DOCKER_BUILDKIT 设为 0，
#     构建会失败或失去平台语义 —— 本脚本会显式提示。
#   * save / load **不带** `--platform`：服务器实测 API 1.45 < 1.48，该参数不可用；
#     平台由这里钉死，后续用校验代替（保存前、载入后各一次）。
if ($env:DOCKER_BUILDKIT -eq '0') {
    Write-Host '[失败] DOCKER_BUILDKIT=0 会关掉 BuildKit，`docker build --platform` 的语义无法保证。' -ForegroundColor Red
    Write-Host '  请去掉该环境变量后重跑（Docker Desktop 默认就是 BuildKit）。' -ForegroundColor Yellow
    exit 1
}
$engineVersion = (& docker version --format '{{.Server.Version}}') 2>$null
Write-Host ("构建机 Docker Engine 版本：" + $engineVersion)
# 打印 BuildKit / buildx 版本，便于事后对账（缺 buildx 时只提示，不阻断：
# 构建本身会因为 BuildKit 语法缺失而明确失败）。
$buildxVersion = (& docker buildx version) 2>$null
if ($LASTEXITCODE -eq 0 -and $buildxVersion) {
    Write-Host ("构建机 buildx 版本：" + $buildxVersion)
} else {
    Write-Host '[警告] 读不到 `docker buildx version`：请确认 Docker 安装完整（本构建需要 BuildKit）。' -ForegroundColor Yellow
}

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
    Write-Host ('[警告] 没能从 pyproject.toml 读到版本号，退回 ' + $Version + '（预研阶段的预期情况）。' +
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

# --- 校验架构并打印镜像信息与大小 ---------------------------------------------
# 构建期是唯一钉死平台的地方，所以构建完立刻确认一次 Os/Architecture：
# 一旦这里就不是目标平台，后面的 save / load 再校验也只是把问题往后推。
Write-Host ''
Write-Host '==> 构建结果' -ForegroundColor Cyan
$badPlatform = @()
foreach ($tag in @($versionTag, $latestTag)) {
    $osArch = (& docker image inspect --format '{{.Os}}/{{.Architecture}}' $tag) 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $osArch) {
        $badPlatform += ($tag + ' -> 读不到 Os/Architecture')
        continue
    }
    $osArch = ([string]$osArch).Trim()
    Write-Host ("  " + $tag + "  架构 " + $osArch)
    if ($osArch -ne $Platform) { $badPlatform += ($tag + ' -> ' + $osArch) }
}
if ($badPlatform.Count -gt 0) {
    Write-Host ''
    Write-Host ('[失败] 构建出来的镜像不是 ' + $Platform + '，交付链在第一步就错了：') -ForegroundColor Red
    foreach ($item in $badPlatform) { Write-Host ('  - ' + $item) -ForegroundColor Red }
    Write-Host ('  重新构建： .\deploy\build-image.ps1 -Platform ' + $Platform + ' -NoCache') -ForegroundColor Yellow
    Write-Host '  并确认构建机 Docker 的 BuildKit 处于开启状态（不要设 DOCKER_BUILDKIT=0）。' -ForegroundColor Yellow
    exit 1
}

function Format-Size {
    param([long]$Bytes)
    if ($Bytes -ge 1GB) { return ('{0:N2} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N2} MB' -f ($Bytes / 1MB)) }
    return ('{0:N0} KB' -f ($Bytes / 1KB))
}

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
Write-Host 'save-image.ps1 会先校验架构、再 docker save、然后跑归档自检（必须是单平台 linux/amd64）。' -ForegroundColor Cyan
Write-Host '提示：latest 只是开发机上的便利 tag；离线服务器用 .env 里的 VS_EMAIL_FORWARDING_IMAGE' -ForegroundColor Cyan
Write-Host '      指定具体版本 tag（便于回滚），不要依赖 latest。' -ForegroundColor Cyan
exit 0
