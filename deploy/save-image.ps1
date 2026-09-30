#Requires -Version 5.1
<#
.SYNOPSIS
    在**开发机**（Windows）上把交付所需的镜像打包成 tar，并输出 sha256 校验值。

.DESCRIPTION
    一次 docker save 会把这些镜像写进同一个 tar：
      * vs-email-forwarding:<版本>   （应用镜像本体）
      * vs-email-forwarding:latest   （便利 tag，让 compose.yaml 的默认镜像名开箱可用）
      * postgres:17         （**必须一起打包**，见下面的说明）
      * almalinux:8         （仅在 -IncludeRehearsal 时，用于 CentOS 8 离线部署预演）

    为什么必须把基础镜像一起打包：
      离线服务器**拉不到任何镜像**（没有 Docker Hub、没有 ghcr.io、没有内网 registry）。
      `docker load` 只恢复 tar 里有的东西；如果 tar 里只有应用镜像，服务器在
      `docker compose up -d` 时发现本地没有 postgres:17，就会尝试去网上拉 —— 直接失败。
      所以凡是 compose.yaml 里出现的 image，都必须在这一个 tar 里带齐。

.PARAMETER Version
    应用镜像版本号。不传时从仓库根 pyproject.toml 的 [project] version 读取，
    读不到退回 0.0.0-dev（预研阶段的预期情况）。

.PARAMETER ImageName
    应用镜像仓库名，默认 vs-email-forwarding。

.PARAMETER OutDir
    输出目录，默认 <仓库根>\dist。dist/ 已在 .gitignore 里，
    这里**只创建目录不创建进仓库的文件**（tar 属于交付物，不该进版本库）。

.PARAMETER IncludeRehearsal
    额外打包 almalinux:8（CentOS 8 离线部署预演用，见 deploy/centos8-rehearsal/）。
    只在确实要演练时加，避免交付包白白大几百 MB。

.PARAMETER Platform
    目标架构，默认 linux/amd64。平台由**构建期**（deploy/build-image.ps1 的
    `docker build --platform`）钉死；本脚本**不再把它传给 docker save**
    （原因见下），只拿它做「保存前校验架构」的判据。

.EXAMPLE
    .\deploy\save-image.ps1
    .\deploy\save-image.ps1 -Version 0.2.0 -IncludeRehearsal

.NOTES
    前置条件：
      1) 已用 deploy/build-image.ps1 构建过应用镜像（构建期已把平台钉死为 linux/amd64）；
      2) 本机已有 postgres:17（`docker pull --platform linux/amd64 postgres:17`）；
      3) 本机有可用的 Python（优先 3.13，只用标准库）——保存后会调用
         deploy/check-image-archive.py 做一次**不依赖 Docker** 的归档自检。

    为什么 docker save **不带** --platform（这是本脚本刻意的选择）：
      * `--platform` 需要 Docker v28+（API 1.48）。服务器实测 Client / Server 均为
        26.1.3、API 1.45，**用不了**这个参数；本脚本虽然跑在开发机上，但交付链不再
        让任何一环依赖它 —— 平台已经在构建期钉死，这里用「保存前校验架构」代替；
      * 交付物必须是**单平台 amd64 归档**：服务器 Engine 26 默认使用 classic image
        store，而 classic image store 不支持 image index / manifest list，多平台归档
        `docker load` 会失败；
      * 若将来服务器 Engine 升到 >= 28，该参数才可用，但本项目**不依赖**它。
#>
[CmdletBinding()]
param(
    [string]$Version,
    [string]$ImageName = 'vs-email-forwarding',
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
    # 注意：这里的 --platform 是**开发机上**的 docker pull，开发机 Docker Desktop 通常
    # 是 v28+，该参数可用；服务器侧不使用 pull（离线），也不使用 --platform。
    Write-Host ('  基础镜像： docker pull --platform ' + $Platform + ' postgres:17')
    if ($IncludeRehearsal) {
        Write-Host ('  预演镜像： docker pull --platform ' + $Platform + ' almalinux:8')
    }
    exit 1
}

# --- 保存前校验：目标镜像必须真的是 linux/amd64 -------------------------------
# 为什么用「校验」代替 `docker save --platform`：
#   1) `--platform` 需要 Docker v28+（API 1.48）；服务器实测 Client / Server 均为
#      26.1.3、API 1.45，用不了该参数 —— 交付链刻意不让任何一环依赖它；
#   2) 平台真正的保障是**构建期**（deploy/build-image.ps1 的 `docker build --platform`）；
#   3) 这里在保存前逐个确认镜像的 Os/Architecture，等价于「显式声明平台」的效果，
#      而且顺带能挡住「开发机是 arm64 时静默存出错架构镜像」这类事故。
Write-Host ''
Write-Host ('==> 保存前校验镜像架构（要求全部为 ' + $Platform + '）') -ForegroundColor Cyan
$badPlatform = @()
foreach ($img in $images) {
    $osArch = (& docker image inspect --format '{{.Os}}/{{.Architecture}}' $img) 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $osArch) {
        $badPlatform += ($img + ' -> 读不到 Os/Architecture')
        continue
    }
    $osArch = ([string]$osArch).Trim()
    Write-Host ('    ' + $img + '  ' + $osArch)
    if ($osArch -ne $Platform) { $badPlatform += ($img + ' -> ' + $osArch) }
}
if ($badPlatform.Count -gt 0) {
    Write-Host ''
    Write-Host ('[失败] 以下镜像不是 ' + $Platform + '，不能打包：') -ForegroundColor Red
    foreach ($item in $badPlatform) { Write-Host ('  - ' + $item) -ForegroundColor Red }
    Write-Host ''
    Write-Host '处置（不要在 save 阶段想办法绕过去）：' -ForegroundColor Yellow
    Write-Host ('  应用镜像：重新用 .\deploy\build-image.ps1 -Platform ' + $Platform + ' 构建（构建期钉死平台）。')
    Write-Host ('  基础镜像：docker pull --platform ' + $Platform + ' postgres:17 后重跑。')
    exit 1
}

# --- 打包 ---------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$tarName = "${ImageName}-images-${Version}.tar"
$tarPath = Join-Path $OutDir $tarName

# 刻意**不加** `--platform`（理由见文件头 .NOTES 与上面的校验步骤）：
# 平台已由构建期钉死、保存前已逐个校验过架构，所以产物就是单平台 amd64 归档。
Write-Host ''
Write-Host ("==> docker save（" + $images.Count + " 个镜像 -> " + $tarPath + "，不带 --platform）") -ForegroundColor Cyan
foreach ($img in $images) { Write-Host ('    - ' + $img) }

& docker save --output $tarPath @images
if ($LASTEXITCODE -ne 0) {
    Write-Host '[失败] docker save 失败。' -ForegroundColor Red
    Write-Host '常见原因：镜像名/标签写错、磁盘空间不足、docker 守护进程异常。' -ForegroundColor Yellow
    exit $LASTEXITCODE
}

# --- 保存后自检：归档必须是单平台（不依赖 Docker）-----------------------------
# 开发机用 Docker Desktop，可能是 containerd image store；那种模式下 docker save
# 可能导出带 manifest list / attestation 的 image index，而服务器的 classic image
# store **不支持** image index，docker load 会失败。这个风险在开发机上用 docker
# 命令看不出来，所以用 Python 标准库读 tar 做自检（deploy/check-image-archive.py）。
$checkScript = Join-Path $PSScriptRoot 'check-image-archive.py'
if (-not (Test-Path -LiteralPath $checkScript)) {
    Write-Host ('[失败] 缺少归档自检脚本：' + $checkScript) -ForegroundColor Red
    Write-Host '  它是交付前的必需环节（用来发现多平台/带 attestation 的归档），不能跳过。' -ForegroundColor Yellow
    Write-Host '  请从版本库恢复 deploy/check-image-archive.py 后重跑本脚本。' -ForegroundColor Yellow
    exit 1
}

$pythonCandidates = @(
    @('py', '-3.13'),
    @('py', '-3'),
    @('python'),
    @('uv', 'run', '--no-project', 'python')
)
$pythonExe = $null
$pythonPrefix = @()
foreach ($candidate in $pythonCandidates) {
    if (-not (Get-Command $candidate[0] -ErrorAction SilentlyContinue)) { continue }
    $prefix = @()
    if ($candidate.Count -gt 1) { $prefix = $candidate[1..($candidate.Count - 1)] }
    & $candidate[0] @prefix '--version' *> $null
    if ($LASTEXITCODE -eq 0) {
        $pythonExe = $candidate[0]
        $pythonPrefix = $prefix
        break
    }
}
if (-not $pythonExe) {
    Write-Host '[失败] 找不到可用的 Python（自检脚本只用标准库，3.8+ 即可）。' -ForegroundColor Red
    Write-Host '  归档自检是交付前必须跑的一步：先装好 Python 或 uv，再重跑本脚本。' -ForegroundColor Yellow
    exit 1
}

Write-Host ''
Write-Host '==> 保存后自检：归档是否为单平台 linux/amd64（不依赖 Docker）' -ForegroundColor Cyan
& $pythonExe @pythonPrefix $checkScript $tarPath --expected-images $images.Count
if ($LASTEXITCODE -ne 0) {
    Write-Host ''
    Write-Host '[失败] 归档自检不通过：这个 tar 不能交付。' -ForegroundColor Red
    Write-Host '  判读标准与处置见上面自检输出，以及 deploy/README.md「交付前归档自检」一节。' -ForegroundColor Yellow
    Write-Host ('  已生成的 tar 保留在 ' + $tarPath + ' 供排查，但**不要**随包发出。') -ForegroundColor Yellow
    exit $LASTEXITCODE
}

# --- sha256 + 体积（归档自检通过后才走这里） ----------------------------------
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
Write-Host '  * 归档自检在生成 sha256 **之前**跑：自检不过就不会产出 .sha256，避免给不合格的包盖章。'
Write-Host '  * 传输后必须在服务器上重新校验 sha256（deploy/load-image.sh 会自动做）。'
Write-Host '  * 交付时除了这个 tar，还要带：compose.yaml、.env（从 .env.example 填真实值）、'
Write-Host '    deploy/ 下的脚本（含 load-image.sh / check-image-archive.py）。清单见 deploy/README.md。'
exit 0
