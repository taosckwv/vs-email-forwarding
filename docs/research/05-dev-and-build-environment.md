# 05 开发与构建环境

> **本册是「开发机、构建环境与本机侧执行前提」的当前有效结论真源。**
> 只写：当前结论、当前实测证据、已否决方向、落地位置、未定项。

## 结论

### 开发机是 CentOS 8 上的 Linux 虚拟机，**装得了 Docker**

开发机为 **CentOS Linux 8.5.2111**（VMware 虚拟机，内核 `4.18.0-348.el8.x86_64`），8 vCPU / 15 GiB 内存 / `/` 36G。SELinux **Enforcing**、firewalld **active**。**没有独立 `/data` 盘。**

它的定位是**「与生产类似的 Linux 服务器，用来编码和做完整测试」**——不是文档里的生产服务器（生产是 CentOS 8.2.2004 + 独立 `/data` 盘，见 `docs/research/01-delivery-and-deploy.md`）。

**这台机器上正式 Docker Engine 可用**，因此**开发依赖的容器栈能在本机起、`tests/integration/` 的容器依赖能在本机跑**。Linux 上的 Docker 不需要硬件虚拟化（容器只用内核 namespaces / cgroups），这是它相对旧 Windows 开发机的根本差别。

#### 项目命令一律以 `tao` 身份执行

DSH 会话与 bash 工具**以 root 运行**，而仓库与 `.venv` 属主是 **`tao`**。

- 一切项目命令（`uv`、`pytest`、门禁脚本、`scripts/dev-up.sh`）都要包一层：

  ```bash
  runuser -u tao -- bash -lc 'cd /home/tao/code/vs-email-forwarding && <命令>'
  ```

- **不这样做会写出 root 属主的 `.venv`**，之后 tao 自己反而用不了。
- `dnf` / `systemctl` 等系统操作用 root。

#### `python3` 是 3.13，系统 3.6 仍在但**绝不可删**

- `uv` 是 **0.13.0**，装在 `/home/tao/.local/bin/uv`（属主 tao）。它**在 tao 的 PATH 上**。
- 系统 `python3` → `/usr/local/bin/python3` → `python3.13` → `/usr/local/share/uv-python/cpython-3.13-linux-x86_64-gnu/bin/python3.13`。root 与 tao 都是 **3.13.16**，与 `.python-version` 的 `3.13` 一致。
- 项目虚拟环境由 uv 托管（`/home/tao/.local/share/uv/python/`）供 `.venv` 使用。
- **`/usr/bin/python3` 仍是 3.6.8**，那是 `platform-python3.6`。**dnf 依赖它，删不得**（`python3-dnf` 硬依赖 `/usr/libexec/platform-python` + `python(abi) = 3.6`）。
- **已知代价**：`python3` 现在是 3.13，而 `python3-gobject` 只有 3.6 ⇒ `ibus-setup`、`cockpit-desktop` 会报 `No module named 'gi'`（中文输入法本体不受影响，仅其设置界面）。回滚：`rm -f /usr/local/bin/python3 /usr/local/bin/python3.13`。

#### 网络姿态（**与生产相反**）

- **有公网**：`pypi.org`、`astral.sh`、`github.com`、`ghcr.io`、`quay.io` 均可达。
- **Docker Hub 直连不可用**：`registry-1.docker.io` 被 DNS 污染（解析到 Meta 的 IPv6 地址 `2a03:2880:f117:83:face:b00c:0:25de`）⇒ **Docker Hub 的镜像必须靠 registry mirror 才能拉**。
- **`ghcr.io` 与 `quay.io` 可达，不经 mirror 也能拉**。
- yum 源：aliyun 的 `CentOS-8.5.2111`（base / AppStream / extras）+ `docker-ce`（见下）。**未启用 EPEL。**

#### Docker 的安装形态（**版本对齐生产**）

- **钉 `docker-ce-26.1.3` + `docker-ce-cli-26.1.3`**，因为**生产就是 26.1.3 / API 1.45**，交付链的结论（`--platform` 需 API 1.48、`docker save` 的归档格式、classic image store）都建立在它之上。**装最新版（28+）会让本机预演失去参考价值**——`docker save` 会产 OCI 格式、containerd image store 成为默认。
- 配套版本：`containerd.io 1.6.32`、`docker-buildx-plugin 0.14.0`、`docker-compose-plugin 2.27.0`。
- **实测与生产一致**：`Storage Driver=overlay2` ⇒ classic image store；`CgroupVersion=1`。生产侧同一读数见 `docs/research/01-delivery-and-deploy.md`。
- **安装源走国内镜像**：Docker 官方 el8 仓库**不可用**（`download.docker.com/linux/centos/8/.../repomd.xml` 报 `SSL_ERROR_SYSCALL`），与「Docker 官方已不再支持 CentOS 8 作宿主」一致。改用 aliyun：`/etc/yum.repos.d/docker-ce.repo` 指向 `mirrors.aliyun.com/docker-ce`，仓库 id 是 **`docker-ce-stable`**（不是 `docker-ce`），`$releasever` 解析为 **`8`**。
- **`podman` 与 `podman-docker` 已卸载**，原因见「已否决方向」。**注意这与 `docs/research/01-delivery-and-deploy.md` 里「podman 保留为将来可能的替代」不冲突**：被否决的是**本机并存**，生产宿主上 podman 天然可得这一点不变。

#### registry mirror 配置

`/etc/docker/daemon.json`（改完要 `systemctl restart docker.service`）：

```json
{
  "registry-mirrors": [
    "https://docker.1panel.live",
    "https://docker.1ms.run"
  ]
}
```

- 顺序**有依据**：`docker.1panel.live` 是**端到端实测可用**的那个（见「实测证据」）；`docker.1ms.run` 只做后备。
- **mirror 只对 Docker Hub 生效**：`docker pull hello-world` 会自动经 mirror；但 `docker pull docker.1panel.live/library/hello-world` 这种把 mirror 当 registry 主机名的写法是另一条路径，**不要混用**。
- **`registry.cn-hangzhou.aliyuncs.com` 不是 Docker Hub 的 mirror**，而是另一个完整 registry，写进 `registry-mirrors` 是错的。
- 某个 mirror 拉不动时：改 `daemon.json` 换下一个 → `systemctl restart docker.service` → 重试。**不要**去第三方站点手工下 rpm 或镜像 tar 硬塞。

#### tao 必须在 `docker` 组里

- Docker socket 是 `root:docker` `srw-rw----`，而**项目命令以 tao 跑** ⇒ tao 不在 `docker` 组里时，`scripts/dev-up.sh` 会走到「连不上 Docker 守护进程」分支并以非零退出。
- 已执行 `usermod -aG docker tao`。
- **有效时机是下一次登录**（或任何新建会话）。**同一会话内 `id` 不会显示新组**，别据此误判失败；需要在不重登的情况下验证，用 `sg docker -c '<命令>'`。

#### 开发依赖（本机现在起得来）

- 开发依赖由 `compose.dev.yaml` 定义，只有两个服务：`postgres:17` 与 `axllent/mailpit:v1.31.3`。Mailpit 同时提供 SMTP / IMAP / Web UI，用于验证「发出去 → 被拉回来 → 关联上原邮件」的完整回路；两者都**只绑回环**。
- 一键起依赖：`scripts/dev-up.sh`（Linux）。应用本体**不在容器里跑**，用 `uv run uvicorn vs_email_forwarding.main:app --reload` 在宿主机跑。
- **两个镜像均已在开发机上实测拉取成功**（`postgres:17` 与 `axllent/mailpit:v1.31.3`）。
- 交付链需要的三个基础镜像**也都能在本机拉到**：`python:3.13-slim-trixie`（经 mirror）、`ghcr.io/astral-sh/uv:0.12.20`（直连 ghcr.io）、`postgres:17`（经 mirror）。**但这不等于「本机应承担构建」**——那条属未定项，见下。
- Mailpit 是否最终采用**仍是未定项**（见「未定项」）。

### 本机侧的执行环境

- 门禁的四步定义与取舍见 `docs/research/03-dependencies-and-toolchain.md`。
- 本册只记**本机特有的执行前提**：`uv` 在 tao 的 PATH 上、`python3` 是 3.13、项目命令一律以 tao 跑、`.venv` 属主必须是 tao。
- **不要给 `scripts/check.sh` 加 `2>&1` 或任何流重定向**：多一层重定向只会掩盖真实退出码，`AGENTS.md` §3 已把它写成脚本约定。
- **pre-commit 钩子在本机尚未安装**（`pre-commit install` 没跑过）。pre-commit 只装在项目 `.venv` 里，要用 `uv run pre-commit ...` 调用。
- **本机跑不了 pre-commit 的钩子环境**：`pre-commit run` 首次执行要 `git fetch` GitHub 上的 hook 仓库，而本机 `github.com:443` 直连被拒（`SSL_ERROR_SYSCALL` / connection refused；`codeload.github.com` 与 PyPI 可达）。因此**唯一稳定的门禁入口是 `bash scripts/check.sh`**——它不依赖任何网络。

#### 文件编码与换行符（硬约定，不是风格偏好）

- 一切文本文件（`.sh` / `.bash` / `.py` / `.toml` / `.yaml` / `.yml` / `.json` / `.md` / `.sql` / `Dockerfile` / `.env*`）：强制 `eol=lf`、**UTF-8 无 BOM**。
- `.sh` 带 CRLF 进容器会直接报 `bad interpreter`。
- **仓库不含 Windows 脚本**：`.ps1` / `.psm1` / `.bat` / `.cmd` 已删除，`.gitattributes` 与 `.pre-commit-config.yaml` 里的 CRLF 例外规则也一并去掉。

### 与生产服务器有关的一句

- 本机侧不承担**生产**部署；服务器侧环境事实（宿主规格、Docker 版本、内网源覆盖等）属 `docs/research/01-delivery-and-deploy.md`，**本册不重复**。
- 本机具备参与镜像构建的能力（见上），但**镜像在哪一侧构建与基础镜像 tar 由谁产出，一律留到开发完成后再定**；当前按 `docs/research/01-delivery-and-deploy.md` 的口径执行。

## 实测证据

**机器与环境**

- **发行版与内核**：`CentOS Linux release 8.5.2111`；内核 `4.18.0-348.el8.x86_64`。
- **资源**：8 vCPU；内存 total 15 GiB、available 13 GiB；`/` 为 `/dev/nvme0n1p3` 36G、可用 28G（23% 已用）；**无 `/data` 盘**。
- **安全与网络**：`getenforce` = `Enforcing`；firewalld `active`；`iptables v1.8.4 (nf_tables)`。
- **`uv` 在 tao 的 PATH 上**：`uv --version` → `uv 0.13.0 (x86_64-unknown-linux-gnu)`，路径 `/home/tao/.local/bin/uv`。
- **Python**：`python3 --version` → `3.13.16`（root 与 tao 相同，解析到 `/usr/local/bin/python3`）；`/usr/bin/python3 --version` → `3.6.8`。
- **门禁在本机实跑通过**：以 tao 身份执行 `bash scripts/check.sh`，四步全绿、退出码 0（`1 passed, 1 warning`；warning 是 FastAPI/Starlette 的 `httpx` 弃用提示，属既知事项，见 `docs/open-questions.md` E2）。
- **pre-commit 可用但钩子未装**：`uv run pre-commit --version` → `pre-commit 4.6.2`；`.git/hooks/` 下只有 sample。

**Docker**

- **版本**：`docker --version` → `Docker version 26.1.3, build b72abbb`（**不再是 podman 的 Emulate 提示**）；`docker compose version` → `Docker Compose version v2.27.0`。
- **与生产对齐的读数**：`docker info` 实测 `Storage Driver=overlay2`（⇒ classic image store）、`CgroupVersion=1`、Server `26.1.3`。生产侧同一读数见 `docs/research/01-delivery-and-deploy.md`。
- **服务状态**：`docker.service` = `active` + `enabled`；dockerd 启动以来 journald 无 warning 及以上条目。
- **`Registry Mirrors` 生效**：`docker info` 的该段列出 `https://docker.1panel.live/` 与 `https://docker.1ms.run/`。
- **Docker 官方 el8 仓库不可用**：`download.docker.com/linux/centos/8/x86_64/stable/repodata/repomd.xml` 报 curl exit 35 `SSL_ERROR_SYSCALL`（HTTP 000），重试仍如此；aliyun / tuna / ustc 的同一路径均 HTTP 200。
- **`$releasever` 实测解析为 `8`**：`dnf -v repolist --repo=docker-ce-stable` 显示 baseurl 展开为 `https://mirrors.aliyun.com/docker-ce/linux/centos/8/x86_64/stable`。注意 `.../centos/8.5.2111/...` 返回 **404**，即**不能**把 `$releasever` 改成 `8.5.2111`。
- **`containerd.io` 的 obsoletes 与 podman 的 requires 硬冲突**：`docker-ce 26.1.3` 要求 `containerd.io >= 1.6.24`；`containerd.io` 声明 `obsoletes: containerd, runc`，而 `containers-common` 与 `podman` 要求 `runc`（字面包名）。`dnf` 因此必须 `--allowerasing`，实际连带移除 `podman` / `podman-catatonit` / `containers-common` / `podman-docker` 四个包。`crun` 虽是另一个 `oci-runtime` 提供者，但 `containers-common` 要的是**字面 `runc` 包**，所以换 runtime 解决不了。
- **mirror 的端到端验证（不是只看状态码）**：对 `docker.1panel.live` 手工走完 registry v2 协议——`/v2/library/hello-world/manifests/latest` 返回 **HTTP 200** 的 OCI image index（11062 字节，20 个子 manifest），取出子 manifest（HTTP 200）后下载其 layer blob（HTTP 200，2415 字节），**且 blob 的 sha256 与 manifest 里的 digest 校验一致**（`sha256sum -c` 报 OK）。这一步才判定「源真能用」。
- **只凭状态码会误判**：`docker.1ms.run` 的 `/v2/` 返回 401（未认证，属正常），`hub.rat.dev` 返回 302（只是跳转），两者都**不足以**证明能拉取。
- **实际拉取成功（本机实测，全部 exit 0）**：`docker pull hello-world` 且 `docker run --rm hello-world` 打印成功信息；`postgres:17`；`axllent/mailpit:v1.31.3`；`python:3.13-slim-trixie`；`ghcr.io/astral-sh/uv:0.12.20`。
- **tao 无 docker 组时确实会被卡住**：未加组前以 tao 跑 `scripts/dev-up.sh` 直接走「[阻塞] docker 命令存在，但连不上 Docker 守护进程」并以退出码 1 结束；`usermod -aG docker tao` 后（以 `sg docker` 等效验证）`docker info` 成功。
- **`dev-up.sh` 现在能过前置检查**：以 tao 身份（带 docker 组）执行 `bash scripts/dev-up.sh` 走到 `docker compose -f compose.dev.yaml up -d` 这一步，退出码 0。验证时用 PATH 前置的探测 shim **只拦截 `up`**、其余透传，因此**没有真的起容器**——`compose.dev.yaml` 的 Mailpit 未拍板，不该由本次任务替它决定。
- **`compose.dev.yaml` 能被 compose v2 解析**：`docker compose -f compose.dev.yaml config --quiet` 退出码 0；解析出的两个服务与 `platform: linux/amd64` 与文件内容一致。

**digest 漂移（交付链要用的读数）**

- **`ghcr.io/astral-sh/uv:0.12.20` 与册中记录一致**：本机 `RepoDigests` 为 `sha256:100047e74f30778ab704942321a09750d6158739573ff58bf3924085cc6cd2d8`，与 `docs/research/01-delivery-and-deploy.md` 记录的 `sha256:100047e7…` 同前缀。
- **`python:3.13-slim-trixie` 的 digest 已漂移**：本机拉到的 `RepoDigests` 是 `sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f`，而 01 册记录的是 `sha256:37134a49d21d2120e4c4d73bb76f8a4ab9aef31f096f7ec2ad48c2feead4332`。**即「浮动 tag 与 digest 会随时间漂移」在锁定 codename 的 tag 上同样成立**，P1 的复核不是形式。**结论：写进交付脚本之前必须重新复核，不要沿用任一册里的旧 digest。**

## 已否决方向

- **在本机让 `podman` 与 Docker 并存** —— 原因：`docker-ce` 硬依赖 `containerd.io`，而 `containerd.io` 的 `obsoletes: runc` 与 `containers-common` / `podman` 的 `requires: runc` 硬冲突，`dnf` 无解。要么卸 podman，要么装不上 Docker，**没有第三条路**；换 `crun` 也不成立（`containers-common` 要的是字面 `runc` 包）。
- **在开发机上装 Docker Desktop（旧 Windows 开发机）** —— 那台机器是未暴露嵌套虚拟化的虚拟机，Docker Desktop 的 WSL2 / Hyper-V 后端都依赖硬件虚拟化与 SLAT，**装不了**（不是「还没装」）。**该机器已弃用**，本结论只作为历史前提保留。
- **给那台虚拟机开启嵌套虚拟化** —— 需改宿主 hypervisor 配置，不可控、不在项目权限内。
- **在开发机上装 docker-ce 最新版（28+）** —— 原因：生产是 26.1.3 / API 1.45，装 28+ 会让 `docker save` 的归档格式与 image store 模式与生产分叉，**使本机预演失去意义**。版本对齐是本机存在的理由。
- **把 `$releasever` 改成 `8.5.2111`** —— 原因：`mirrors.aliyun.com/docker-ce/linux/centos/8.5.2111/...` 返回 404，而 `$releasever=8` 实测可用。
- **本机直接 `docker pull registry-1.docker.io/...`** —— 原因：该域名被 DNS 污染，解析到 Meta 的 IPv6 地址，报 connection refused；必须走 registry mirror。
- **把 `registry.cn-hangzhou.aliyuncs.com` 当 Docker Hub mirror** —— 原因：它是完整的独立 registry，不是 Hub 的 mirror，写进 `registry-mirrors` 语义错误。
- **用状态码判断 mirror 是否可用** —— 原因：401 / 302 都只说明端点活着。`docker.1ms.run` 401、`hub.rat.dev` 302 都不足以证明能拉；必须端到端真拉一次（见「实测证据」）。
- **在仓库里保留 Windows 脚本（`scripts/*.ps1`）与配套的 BOM / CRLF 例外规则** —— 原因：开发机已无 Windows，无人运行这些脚本；留着就是一份没人维护、还带特殊编码要求的死代码。已删除 `scripts/check.ps1` 与 `scripts/dev-up.ps1`，并去掉 `.gitattributes` 与 `.pre-commit-config.yaml` 里的 CRLF 例外。

## 落地位置

| 位置 | 内容 |
|---|---|
| `scripts/check.sh` | 门禁入口；四步定义与取舍见 `docs/research/03-dependencies-and-toolchain.md` |
| `scripts/dev-up.sh` | 起开发依赖（读 `compose.dev.yaml`） |
| `compose.dev.yaml` | 开发依赖定义：PostgreSQL + Mailpit，只绑回环 |
| `/etc/docker/daemon.json` | registry mirror（**在机器上，不在仓库里**） |
| `/etc/yum.repos.d/docker-ce.repo` | docker-ce 安装源，指向 aliyun（**在机器上，不在仓库里**） |
| `.gitattributes` | 换行符强制规则（一切文本文件 LF） |
| `.pre-commit-config.yaml` | pre-commit 钩子定义（本机尚未 `install`） |
| `pyproject.toml` | dev 依赖组与工具链配置；依赖清单与门禁配置的结论见 `docs/research/03-dependencies-and-toolchain.md` |
| `deploy/` | 部署脚本与交付链的落点：新链路脚本尚未实现，其形态属未定项 |
| `docs/research/01-delivery-and-deploy.md` | 生产服务器与交付链事实（本册不重复） |
| `docs/open-questions.md` | 未定项的唯一清单 |

## 未定项

详见 `docs/open-questions.md`：

- D6 本地邮件捕获工具（Mailpit）是否采用（本机具备验证条件）
- D13 基础镜像 tar 由哪台机器、用什么工具产出
- 本册另记一条**尚无编号**的未定项：mailpit 的 `MP_*` 环境变量名尚未在本机对容器实跑核对。

D3（开发机的 Docker 能力）**已关闭**：结论已写入本册——开发机跑得了 Docker，不再列为未定项。
