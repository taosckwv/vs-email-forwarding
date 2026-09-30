# 05 开发与构建环境

> **本册是「开发机、构建环境与本机侧执行前提」的当前有效结论真源。**
> 只写：当前结论、当前实测证据、已否决方向、落地位置、未定项。

## 结论

### 开发机是 Windows 上的虚拟机，只能用宿主机原生的 Python + uv 工作

开发机为 Windows 10 专业版 22H2（Build 19045），**本身是一台虚拟机**，且宿主**未暴露嵌套虚拟化**。下面全部约束都由此派生。

#### 调用 `uv`

- **`uv` 不在新进程的 PATH 上**。两种可用写法：
  - 直接用 `%APPDATA%\Python\Python314\Scripts\uv.exe`；
  - 或先把机器级与用户级 PATH 拼起来再用 `uv`：

    ```powershell
    $env:PATH = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
    ```

- `scripts/check.ps1` 自己会检查 `uv` 是否可见；**不拼 PATH 直接调用它会以退出码 127 报「找不到 uv 命令」**——那不是门禁失败，是调用方式不对。
- `uv` 的默认缓存目录在受限环境下可能不可写（`failed to initialize cache ... 拒绝访问 (os error 5)`）。处置：把 `UV_CACHE_DIR` 指到一个可写目录。**这是环境权限问题，不是仓库配置问题。**

#### 每一次 shell 调用都是全新进程、无状态

- `cd`、环境变量、函数都不会带到下一次调用：每次都是新进程。
- 因此要用**工作目录参数**指定目录，**不要指望 `cd` 生效**。
- 同理，PATH 与 `VIRTUALENV_OVERRIDE_APP_DATA` 这类环境变量必须在**同一次调用内**设置。

#### 绝不给 `scripts/check.ps1` 加重定向

- **绝对不要给 `scripts/check.ps1` 加 `2>&1` 或任何流重定向。**
- 原因：`uv` 会往 stderr 打**正常的**构建日志（例如 `Building vs-email-forwarding ...`），Windows PowerShell 5.1 会把它变成 `NativeCommandError`，脚本被中断并报 `[exit code: 1]`。
- **脚本本身没有问题，是重定向的锅。** 直接调用、看 `$LASTEXITCODE`。

#### 提交前绕开 pre-commit 的 Windows 权限问题

- 提交前设 `$env:VIRTUALENV_OVERRIDE_APP_DATA = '%LOCALAPPDATA%\pc-venv-data'`，规避 pre-commit 的 `WinError 5 拒绝访问 ... pip-*.exe`。
- 该失败是**间歇性**的：**重试通常即过**，不必改配置。

#### `.ps1` 必须保存为 UTF-8 with BOM

- 一切 `.ps1`（`scripts/*.ps1` 等）**必须保存为 UTF-8 with BOM**。
- 没有 BOM 时 Windows PowerShell 5.1 会按 **GBK** 解码，中文注释直接导致**语法错误**——**不是显示乱码，是脚本根本跑不起来**。
- 做定点编辑可以保留 BOM；**不要整文件重写**。改完校验前三个字节是 `EF BB BF`。

#### 路径含中文与空格

- 仓库路径本身含中文与空格。**不要用绝对路径去调 `.venv\Scripts\python.exe`**：中文会被 mangle，路径报 `CommandNotFoundException`。
- 用**相对路径**：`.\.venv\Scripts\python.exe`。

#### Windows PowerShell 5.1 的其他坑

- `&` **不能当后台运算符**（报 `AmpersandNotAllowed`）。
- 量端口属主时，用 `Get-Process` 可能拿到 launcher 而不是真实监听进程——**按端口属主去查**。

#### 文件编码与换行符（硬约定，不是风格偏好）

- `.ps1` / `.psm1` / `.bat` / `.cmd`：强制 `eol=crlf`。
- 其余文本（`.sh` / `.py` / `.toml` / `.yaml` / `.yml` / `.json` / `.md` / `.sql` / `Dockerfile` / `.env*`）：强制 `eol=lf`。
- 除 `.ps1` 系列外，一律 **UTF-8 无 BOM + LF**。
- `.sh` 带 CRLF 进容器会直接报 `bad interpreter`。

#### 会话工具

- `present` 一次最多接受 8 个文件。

### 开发机装不了 Docker Desktop（是「装不了」，不是「没装」）

- 开发机是**未暴露嵌套虚拟化的虚拟机**：Windows 10 22H2（Build 19045），`HypervisorPresent=True`，但 `VirtualizationFirmwareEnabled`、SLAT（`SecondLevelAddressTranslationExtensions`）、`VMMonitorModeExtensions` 均为 False（CPU 被掩码为 T7700），WSL 状态检查退出码 50。
- Docker Desktop 的 **WSL2 与 Hyper-V 两个后端都依赖硬件虚拟化与 SLAT**，因此**在这台机器上都不可用**——不是「尚未安装、装一下就成」。
- 这也是镜像**在生产服务器上本地构建**的原因（交付链见 `docs/research/01-delivery-and-deploy.md`）。
- **限制只卡 Windows 上的 Docker Desktop**：Linux 上的 Docker 不需要硬件虚拟化（容器只用内核 namespaces / cgroups），一台普通 Linux 机器或 Linux 虚拟机即可承担构建与预演。
- **后果**：本机**无法验证任何容器相关链路**；`docker compose` 开发依赖（PostgreSQL + Mailpit）**在本机起不来**；`tests/integration/` 中依赖真实 PostgreSQL / 邮件服务的用例在本机无法执行。

### 开发依赖（受上一条限制，本机起不来）

- 开发依赖由 `compose.dev.yaml` 定义，只有两个服务：`postgres:17` 与 `axllent/mailpit:v1.31.3`。Mailpit 同时提供 SMTP / IMAP / Web UI，用于验证「发出去 → 被拉回来 → 关联上原邮件」的完整回路；两者都**只绑回环**。
- 一键起依赖：`scripts/dev-up.ps1`（Windows）/ `scripts/dev-up.sh`（Linux）。应用本体**不在容器里跑**，用 `uv run uvicorn vs_email_forwarding.main:app --reload` 在宿主机跑。
- **在本机 `dev-up.ps1` 只会打印安装提示并以非零退出码结束**，不会真的起任何东西。
- Mailpit 是否最终采用仍是未定项（见「未定项」）。

### 本机侧的执行环境

- 门禁的四步定义与取舍见 `docs/research/03-dependencies-and-toolchain.md`。
- 本册只记**本机特有的执行前提**：`uv` 不在新进程 PATH 上、`UV_CACHE_DIR` 要指向可写目录、**绝不**给 `scripts/check.ps1` 加流重定向、`.pytest_cache` 的 `WinError 5` 可忽略（见上面「调用 `uv`」与「绝不给 `scripts/check.ps1` 加重定向」两节）。

### pre-commit 在 Windows 上的两个已知问题及处置

- **`pre-commit not found`**：pre-commit 只装在项目的 `.venv` 里（属 `pyproject.toml` 的 dev 依赖组），不在全局 PATH 上。处置：一律用 `uv run pre-commit ...` 调用；装钩子即 `uv run pre-commit install --install-hooks` 与 `uv run pre-commit install --hook-type commit-msg`。
- **`PermissionError: [WinError 5] 拒绝访问 ... pip-*.exe`**：pre-commit 创建钩子环境时偶发。处置：先把 `VIRTUALENV_OVERRIDE_APP_DATA` 指向可写目录（见上），失败后**重试**——该失败是间歇性的。

### 与生产服务器有关的一句

- 本机**不承担镜像构建**：构建在生产服务器上本地完成。服务器侧环境事实（宿主规格、Docker 版本、内网源覆盖等）属 `docs/research/01-delivery-and-deploy.md`，**本册不重复**。
- 与本机相关的一点：**基础镜像 tar 由哪台机器、用什么工具产出仍未定**，而本机装不了 Docker，因此这条路径尚未打通。

## 实测证据

- **`uv` 不在 PATH 上**：新进程里 `Get-Command uv` 为空；`%APPDATA%\Python\Python314\Scripts\uv.exe` 存在。
- **`.ps1` 的 BOM 已核验**：`scripts/check.ps1` 与 `scripts/dev-up.ps1` 的前三个字节均为 `EF BB BF`。
- **门禁在本机实跑通过**（拼好 PATH 后直接调用 `scripts/check.ps1`，无任何重定向，退出码 0）；四步的输出与 ruff 告警口径见 `docs/research/03-dependencies-and-toolchain.md`，本册不重复。
- 同一次 `pytest` 还输出 `PytestCacheWarning: could not create cache path ...\.pytest_cache\...: [WinError 5] 拒绝访问`——本机特有的权限拒绝，**不影响门禁结果**（pytest 仍以 0 退出），忽略即可。
- 为让 `uv` 正常启动，本次实跑把 `UV_CACHE_DIR` 指到了可写的临时目录。不这样做时 `uv` 在初始化默认缓存目录时报 `拒绝访问 (os error 5)` 并以退出码 2 失败，`scripts/check.ps1` 会如实报「[1/4] 失败」——**这是权限问题，与仓库无关**。
- **pre-commit 可用**：`uv run pre-commit --version` → `pre-commit 4.6.2`；`.git/hooks/` 下已装 `pre-commit` 与 `commit-msg` 两个钩子。
- **开发依赖定义**：`compose.dev.yaml` 内含 `postgres:17` 与 `axllent/mailpit:v1.31.3`，两者的宿主端口映射都只绑回环。
- **Docker 不可用的实测读数**：`HypervisorPresent=True`，`VirtualizationFirmwareEnabled=False`、`SecondLevelAddressTranslationExtensions=False`、`VMMonitorModeExtensions=False`，CPU 掩码为 T7700，WSL 状态检查退出码 50。

## 已否决方向

- **在开发机上装 Docker Desktop** —— 这台开发机是未暴露嵌套虚拟化的虚拟机，Docker Desktop 的 WSL2 / Hyper-V 后端都依赖硬件虚拟化与 SLAT，**装不了**（不是「还没装」）。
- **给这台虚拟机开启嵌套虚拟化** —— 需改宿主 hypervisor 配置，不可控、不在项目权限内。
- **在 Windows 上用 PowerShell 重定向捕获 `uv` 的 stderr** —— Windows PowerShell 5.1 会把正常构建日志变成 `NativeCommandError` 并中断脚本。

## 落地位置

| 位置 | 内容 |
|---|---|
| `scripts/check.ps1` / `scripts/check.sh` | 门禁入口；四步定义与取舍见 `docs/research/03-dependencies-and-toolchain.md`，本册只记本机执行前提 |
| `scripts/dev-up.ps1` / `scripts/dev-up.sh` | 起开发依赖（读 `compose.dev.yaml`） |
| `compose.dev.yaml` | 开发依赖定义：PostgreSQL + Mailpit，只绑回环 |
| `.gitattributes` | 换行符强制规则（`.ps1` 系列 CRLF，其余 LF） |
| `.pre-commit-config.yaml` | pre-commit 钩子：文件卫生、ruff、commitizen |
| `pyproject.toml` | dev 依赖组与工具链配置；依赖清单、ruff 告警口径与门禁配置的结论见 `docs/research/03-dependencies-and-toolchain.md` |
| `deploy/` | 部署脚本与交付链的落点：新链路脚本尚未实现，其形态属未定项 |
| `docs/research/01-delivery-and-deploy.md` | 生产服务器与交付链事实（本册不重复） |
| `docs/open-questions.md` | 未定项的唯一清单 |

## 未定项

详见 `docs/open-questions.md`：

- D2 部署脚本的具体形态（新链路脚本尚未实现）
- D6 本地邮件捕获工具（Mailpit）是否采用（受「本机装不了 Docker」制约）

D3（开发机的 Docker 能力）**已关闭**：结论已写入本册，不再列为未定项。
