#Requires -Version 5.1
<#
.SYNOPSIS
    在**开发机**（Windows）上把交付所需的镜像打包成 tar，并输出 sha256 校验值。

.DESCRIPTION
    一次 docker save 会把这些镜像写进同一个 tar：
      * mail-relay:<版本>   （应用镜像本体）
      * mail-relay:latest   （便利 tag，让 compose.yaml 的默认镜像名开箱可用）
      * postgres:17         （**必须一起打包**，见下面的说明）
      * almalinux:8         （仅在 -IncludeRehearsal 时，用于 CentOS 8 离线部署预演）

    为什么必须把基础镜像一起打包：
      离线服务器**拉不到任何镜像**（没有 Docker Hub、没有 ghcr.io、没有内网 registry）。
      `docker load` 只恢复 tar 里有的东西；如果 tar 里只有应用镜像，服务器在
      `docker compose up -d` 时发现本地没有 postgres:17，就会尝试去网上拉 —— 直接失败。
      所以凡是 compose.yaml 里出现的 image，都必须在这一个 tar 里带齐。

.PARAMETER Version
    应用镜像版本号。不传时从仓库根 pyproject.toml 的 [project] version 读取，
    读不到退回 0.0.0-dev（骨架阶段的预期情况）。

.PARAMETER ImageName
    应用镜像仓库名，默认 mail-relay。

.PARAMETER OutDir
    输出目录，默认 <仓库根>\dist。dist/ 已在 .gitignore 里，
    这里**只创建目录不创建进仓库的文件**（tar 属于交付物，不该进版本库）。

.PARAMETER IncludeRehearsal
    额外打包 almalinux:8（CentOS 8 离线部署预演用，见 deploy/centos8-rehearsal/）。
    只在确实要演练时加，避免交付包白白大几百 MB。

.PARAMETER Platform
    目标架构，默认 linux/amd64。必须与 build-image.ps1、compose 的 platform 一致。

.EXAMPLE
    .\deploy\save-image.ps1
    .\deploy\save-image.ps1 -Version 0.2.0 -IncludeRehearsal

.NOTES
    前置条件：
      1) 已用 deploy/build-image.ps1 构建过应用镜像；
      2) 本机已有 postgres:17（`docker pull --platform linux/amd64 postgres:17`）；
      3) Docker Engine >= 28（`docker save --platform` 从 v28 才有）。
         Engine >= 25 起的 save 产出 **OCI 归档**；若服务器用的是 classic image store
         或老引擎，load 可能失败 —— 上线前务必按 deploy/README.md 核对服务器版本。
#>
[CmdletBinding()]
param(
    [string]$Version,
    [string]$ImageName = 'mail-relay',
    [string]$OutDir,
    [string]$Platform = 'linux/amd64',
    [switch]$IncludeRehearsal
)

$ErrorActionPreference = 'Stop'
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host '[阻塞] 找不到 docker 命令，请先安装并启动 Docker Desktop。' -ForegroundColor Red
    exit 127
}

# --- 解析版本号（与 build-image.ps1 同一套规则） -------------------------------
if (-not $Version) {
    $pyproject = Join-Path $repoRoot 'pyproject.toml'
    if (Test-Path -LiteralPath $pyproject) {
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
    Write-Host ('[警告] 没能读到版本号，使用 ' + $Version + '；正式交付请用 -Version 指定。') -ForegroundColor Yellow
}

if (-not $OutDir) { $OutDir = Join-Path $repoRoot 'dist' }

# --- 待打包镜像清单 -----------------------------------------------------------
$images = @(
    "${ImageName}:${Version}",
    "${ImageName}:latest",
    'postgres:17'
)
if ($IncludeRehearsal) {
    # 预演容器镜像：默认 almalinux:8（活跃维护、glibc 2.28、dnf 源开箱可用）。
    # 备选 rockylinux/rockylinux:8；**不要**用 library/rockylinux:8（停在 8.9）。
    $images += 'almalinux:8'
}

# 先确认镜像都在本地，缺哪个就明确告诉用户怎么补，不要等到 docker save 才报错
$missing = @()
foreach ($img in $images) {
    & docker image inspect $img *> $null
    if ($LASTEXITCODE -ne 0) { $missing += $img }
}
if ($missing.Count -gt 0) {
    Write-Host '[失败] 以下镜像不在本地，无法打包：' -ForegroundColor Red
    foreach ($img in $missing) { Write-Host ('  - ' + $img) -ForegroundColor Red }
    Write-Host ''
    Write-Host '补齐方式：' -ForegroundColor Yellow
    Write-Host ('  应用镜像： .\deploy\build-image.ps1 -Version ' + $Version)
    Write-Host ('  基础镜像： docker pull --platform ' + $Platform + ' postgres:17')
    if ($IncludeRehearsal) {
        Write-Host ('  预演镜像： docker pull --platform ' + $Platform + ' almalinux:8')
    }
    exit 1
}

# --- 打包 ---------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$tarName = "${ImageName}-images-${Version}.tar"
$tarPath = Join-Path $OutDir $tarName

# --platform linux/amd64：显式声明要保存哪个平台的镜像。
#   自 Docker Engine v25 起 docker save 产出 OCI 归档；--platform 需要 v28+。
#   显式写平台可以防止「开发机是 arm64 时静默存出错架构镜像，服务器 load 后起不来」。
Write-Host ''
Write-Host ("==> docker save（平台 " + $Platform + "，" + $images.Count + " 个镜像 -> " + $tarPath + "）") -ForegroundColor Cyan
foreach ($img in $images) { Write-Host ('    - ' + $img) }

& docker save --platform $Platform --output $tarPath @images
if ($LASTEXITCODE -ne 0) {
    Write-Host '[失败] docker save 失败。' -ForegroundColor Red
    Write-Host '常见原因：本机 Engine < v28（不支持 --platform）。此时需要升级 Docker Desktop。' -ForegroundColor Yellow
    exit $LASTEXITCODE
}

# --- sha256 + 体积 ------------------------------------------------------------
function Format-Size {
    param([long]$Bytes)
    if ($Bytes -ge 1GB) { return ('{0:N2} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N2} MB' -f ($Bytes / 1MB)) }
    return ('{0:N0} KB' -f ($Bytes / 1KB))
}

$file = Get-Item -LiteralPath $tarPath
$hash = (Get-FileHash -LiteralPath $tarPath -Algorithm SHA256).Hash.ToLower()

# 同时写一个 .sha256 旁路文件，随 tar 一起传到服务器，供 deploy/load-image.sh 校验。
# 写成 "<hash>  <文件名>"（两个空格）+ LF，与 Linux 的 sha256sum -c 兼容；
# 这里用 -NoNewline 是为了不引入 CRLF（CRLF 会让 sha256sum 把 \r 当成文件名的一部分）。
$shaFile = "$tarPath.sha256"
Set-Content -LiteralPath $shaFile -Value ("$hash  $tarName") -NoNewline -Encoding Ascii

Write-Host ''
Write-Host '==> 交付物已生成' -ForegroundColor Cyan
Write-Host ('  文件   : ' + $file.FullName)
Write-Host ('  大小   : ' + (Format-Size -Bytes $file.Length) + '（' + $file.Length + ' 字节）')
Write-Host ('  sha256 : ' + $hash)
Write-Host ('  校验文件: ' + $shaFile)
Write-Host ''
Write-Host '注意事项：' -ForegroundColor Yellow
Write-Host '  * dist/ 已在 .gitignore 里：tar 是交付物，不进版本库。'
Write-Host '  * 传输后必须在服务器上重新校验 sha256（deploy/load-image.sh 会自动做）。'
Write-Host '  * 交付时除了这个 tar，还要带：compose.yaml、.env（从 .env.example 填真实值）、'
Write-Host '    deploy/ 下的脚本。清单见 deploy/README.md。'
exit 0
