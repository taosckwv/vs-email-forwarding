# 05 开发与构建环境

> **本册是「开发机、构建环境与本机侧执行前提」的当前有效结论真源。**
> 只写：当前结论、当前实测证据、已否决方向、落地位置、未定项。

## 结论

### 开发机是 CentOS 8 上的 Linux 虚拟机，**装得了 Docker**

开发机为 **CentOS Linux 8.5.2111**（VMware 虚拟机，内核 `4.18.0-348.el8.x86_64`），8 vCPU / 15 GiB 内存 / `/` 36G。SELinux **Enforcing**、firewalld **active**。**没有独立 `/data` 盘。**

它的定位是**「与生产类似的 Linux 服务器，用来编码和做完整测试」**——不是文档里的生产服务器（生产是 CentOS 8.2.2004 + 独立 `/data` 盘，见 `docs/research/01-delivery-and-deploy.md`）。

**这台机器上正式 Docker Engine 可用**，因此**开发依赖的容器栈能在本机起、`tests/integration/` 的容器依赖能在本机跑**。Linux 上的 Docker 不需要硬件虚拟化（容器只用内核 namespaces / cgroups），这是它相对旧 Windows 开发机的根本差别。

#### 项目命令一律以 `root` 身份执行

DSH 会话与 bash 工具**以 root 运行**，仓库与 `.venv` 的属主**也是 root**；所有者已明确要求**项目命令与文件操作直接用 root**——
不要再用 `runuser -u tao`（该口径已于 2026-10 被取代）。

- 一切项目命令（`uv`、`pytest`、门禁脚本、`scripts/dev-up.sh`）直接以 root 执行：

  ```bash
  cd /home/tao/code/vs-email-forwarding && <命令>
  ```

- **一个前提要自己补上**：`uv` 装在 `/home/tao/.local/bin/`，而**该目录不在 root 的 PATH 上**。
  root 会话里要么显式 `export PATH=/home/tao/.local/bin:$PATH`，要么直接写绝对路径
  `/home/tao/.local/bin/uv ...`；直接用 `uv` 会报 command not found。
- `dnf` / `systemctl` 等系统操作用 root（与项目命令同一个身份）。

#### `python3` 是 3.13，系统 3.6 仍在但**绝不可删**

- `uv` 是 **0.13.0**，装在 `/home/tao/.local/bin/uv`。该目录只加在 tao 的 PATH 上，**root 会话要用就显式引入**（见上文）。
- 系统 `python3` → `/usr/local/bin/python3` → `python3.13` → `/usr/local/share/uv-python/cpython-3.13-linux-x86_64-gnu/bin/python3.13`。**3.13.16**，与 `.python-version` 的 `3.13` 一致。
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

#### 开发依赖（本机现在起得来）

- 开发依赖由 `compose.dev.yaml` 定义，只有两个服务：`postgres:17` 与 **`greenmail/standalone:2.1.14`**；两者都**只绑回环**。GreenMail 同时提供 **SMTP（3025）与 IMAP（3143）**，因此流②的发信侧与**流③的入站回路都能在本地离线跑通**（另有 POP3 3110、SMTP API 8080；SMTPS 3465 / IMAPS 3993 未映射）。
- 一键起依赖：`scripts/dev-up.sh`（Linux）。应用本体**不在容器里跑**，用 `uv run uvicorn vs_email_forwarding.main:app --reload` 在宿主机跑。
- **两个镜像均已在开发机上实测拉取成功**（`postgres:17` 与 `greenmail/standalone:2.1.14`）。
- 交付链需要的三个基础镜像**也都能在本机拉到**：`python:3.13-slim-trixie`（经 mirror）、`ghcr.io/astral-sh/uv:0.12.20`（直连 ghcr.io）、`postgres:17`（经 mirror）。**但这不等于「本机应承担构建」**——那条属未定项，见下。
- **邮件服务用 GreenMail，不用 Mailpit**（所有者拍板，2026-10；`D16` 由此关闭，`D6`「是否采用 Mailpit」被取代并移除）。**Mailpit 不提供 IMAP**（只有 SMTP + Web UI + POP3，上游把 IMAP 的 issue 以 `not_planned` 关闭）⇒ 它**不可能**同时覆盖收发两个协议，用它就跑不通「发出去 → 被拉回来 → 关联上原邮件」。**被否决方向：用 Mailpit 做本地回路验证**（证据见「实测证据」）。
- GreenMail 的三个实测行为要记住：① **按账号分信箱**——投给 `reply@example.com` 的信要用该账号登录才看得到（正好贴合 `IMAP_USERNAME` 的用法）；② **消息只活在容器生命周期里**（没挂卷，`down` 即清空，集成测试不必担心上一轮残留）；③ **端口与 Mailpit 完全不同**（SMTP 3025 / IMAP 3143），`.env` 与文档里的 `1025/1143` 一律作废。
- GreenMail 官方镜像**没有 healthcheck 命令**（镜像里只有 JRE 与一个启动脚本），所以 `compose.dev.yaml` 里不给它写 healthcheck；判断它好了没有看容器日志里的 `Starting GreenMail standalone v2.1.14 ... protocol='imap'`。

### 本机侧的执行环境

- 门禁的五步定义与判据见 `docs/research/03-dependencies-and-toolchain.md`；脚本侧说明见 `scripts/README.md`。
- 本册只记**本机特有的执行前提**：项目命令以 root 跑、`uv` 要显式入 PATH、`python3` 是 3.13。
- **不要给 `scripts/check.sh` 加 `2>&1` 或任何流重定向**：多一层重定向只会掩盖真实退出码，`AGENTS.md` §3 已把它写成脚本约定。
- **pre-commit 钩子已安装**（2026-10）：`.git/hooks/pre-commit` 与 `.git/hooks/commit-msg` 都在，钩子环境已缓存（**缓存按 HOME 分家**：root 用 `/root/.cache/pre-commit/`，从 `/home/tao/.cache/pre-commit/` 整目录拷过去即可离线使用），**日常提交不再需要网络**。pre-commit 只装在项目 `.venv` 里，用 `uv run pre-commit ...` 调用。
- **`github.com:443` 在本机是「时通时断」**：装钩子环境时必须从 GitHub 克隆三个 hook 仓库（`pre-commit-hooks` / `ruff-pre-commit` / `commitizen`），实测一次 `fetch` 被拒（connection refused）、紧接着重试即成功，且同一次会话里 root 侧 `curl https://github.com` 曾 15s 超时。**结论：不要依据一次失败判定 GitHub 不可用，隔几分钟重试即可**；旧记录「github.com:443 直连被拒」与据此得出的「本机跑不了 pre-commit」均不成立。
- **git 已从源码升到 2.49.1，装在 `/usr/local`**（2026-10）：`/usr/local/bin` 在默认 PATH 里排在 `/usr/bin` 之前，所以**任何用户、任何新会话**默认就是 `/usr/local/bin/git`（实测 root 与 tao、登录 shell 都解析到它）。系统包 `git-2.27.0` **没有被卸载、文件也没被改动**（`rpm -V git` 无输出），仍留在 `/usr/bin/git` 作为后备；vault 里没有更新的 git 包，`pre-commit run --all-files` 需要的 `git ls-files --deduplicate`（需 ≥ 2.31）正是靠这次升级才可用。
- **仓库属主已统一成 `root:root`**（2026-10，全仓 5393 项，含 `.git` 与 `.venv`），因此 git ≥ 2.35 的 `dubious ownership` 限制**不再触发**，也**不需要** `safe.directory` 这类豁免（曾经加过的那条已从 `/root/.gitconfig` 移除）。**tao 从此对该仓库没有写权限**——这与「命令一律以 root 跑」是一致的。权限也一并收紧：目录 `755`、文件 `644`（原本带执行位的保持 `755`），**去掉了组写位**（原先 `664/775` 而 `tao` 在 `root` 组里，仅改属主仍挡不住它）。实测 `runuser -u tao -- touch <仓库>/.x` → `Permission denied`。**唯一例外**：`.venv/bin/python*` 与 `.venv/lib64` 是指向仓库外的符号链接，其目标是 uv 在 `/home/tao/.local/share/uv/` 下管理的解释器（仍属 tao），**那些不要动**。

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
- **`uv` 的路径**：`uv --version` → `uv 0.13.0 (x86_64-unknown-linux-gnu)`，装在 `/home/tao/.local/bin/uv`——**该目录只加在 tao 的 PATH 上，root 会话里要显式引入**。
- **Python**：`python3 --version` → `3.13.16`（解析到 `/usr/local/bin/python3`）；`/usr/bin/python3 --version` → `3.6.8`。
- **`pre-commit run --all-files` 实跑通过（2026-10，git 2.49.1）**：九个钩子全 `Passed`、退出码 0——**这条以前跑不了**（git 2.27 不认 `--deduplicate`），是升级 git 后的直接收益；commit-msg 侧仍由 commitizen 校验。
- **门禁在本机跑得通，而且不需要 Docker**：以 root 身份执行 `bash scripts/check.sh`，**五步全绿、退出码 0**——这台开发机上不必起任何容器（第 5 步的变异测试与存活复验只跑 `tests/unit`）。**门禁的步骤、判据与当次实测数字不在本册**：步骤与判据见 `scripts/README.md`，「什么算合格」与基线数字见 `docs/research/03-dependencies-and-toolchain.md`——本册只记「这台机器上跑得通」这一条环境事实，不写派生快照。
- **pre-commit 版本**：`uv run pre-commit --version` → `pre-commit 4.6.2`；钩子此前只有 sample，已按上文安装完毕。

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
- **实际拉取成功（本机实测，全部 exit 0）**：`docker pull hello-world` 且 `docker run --rm hello-world` 打印成功信息；`postgres:17`；`greenmail/standalone:2.1.14`（310MB，见下）；`python:3.13-slim-trixie`；`ghcr.io/astral-sh/uv:0.12.20`。
- **docker 权限随执行身份**：root 直接可用；早先以 tao 跑时因不在 `docker` 组里走过「[阻塞] docker 命令存在，但连不上 Docker 守护进程」（那次已用 `usermod -aG docker tao` 解决）。**现行口径是命令一律以 root 跑，这条不再影响本项目。**
- **`dev-up.sh` 曾以探测 shim 只验证过前置检查**（拦 `up` 不真起容器，退出码 0）。**后续已改为真实跑通**，见下面「`scripts/dev-up.sh` 实跑成功」。
- **`compose.dev.yaml` 能被 compose v2 解析**：`docker compose -f compose.dev.yaml config --quiet` 退出码 0；解析出的两个服务与 `platform: linux/amd64` 与文件内容一致。
- **`scripts/dev-up.sh` 实跑成功（2026-10，真实起容器）**：`docker compose -f compose.dev.yaml ps` 两个服务都 `Up (healthy)`；Mailpit Web UI `curl` 返回 **200**；Mailpit `/api/v1/info` 返回 `{"Version":"v1.31.3", ...}`；PostgreSQL `pg_isready` → `accepting connections`，`select version()` → `PostgreSQL 17.11`，开发库 `vs_email_forwarding_dev` 已自动建出（测试库仍需手工建）；四个端口（5432 / 1025 / 1143 / 8025）都只绑 `127.0.0.1`。
- **Mailpit 的 `MP_*` 变量与 IMAP 能力实测**：容器内 `env` 确认 `MP_SMTP_AUTH_ACCEPT_ANY`、`MP_SMTP_AUTH_ALLOW_INSECURE`、`MP_IMAP_AUTH_ACCEPT_ANY`、`MP_IMAP_AUTH_ALLOW_INSECURE` 都传进去了。用一次性 venv（`imaplib` + `smtplib`）做真实往返：**SMTP 侧任意凭据 `AUTH PLAIN` 登录成功并投递成功**（`MP_SMTP_AUTH_*` 生效）。**IMAP 侧连不上**：`imaplib.IMAP4("127.0.0.1", 1143)` 一建连就 `EOF`，裸 socket 收到的 greeting 是空串，而**容器日志只有 `[smtpd]`、`[http]`、`[pop3]` 三类启动行，没有 IMAP 行**。
- **GreenMail 替换 Mailpit 的实测（2026-10）**：`docker pull greenmail/standalone:2.1.14`（310MB，digest `sha256:1ef95a96…`，与 `latest` 相同；**经 mirror 拉取偏慢，实测一次 300s 超时，放后台才拉完**）。容器启动日志列出六个协议与 HTTP API：`smtp 3025`、`smtps 3465`、`pop3 3110`、`pop3s 3995`、`imap 3143`、`imaps 3993`、`GreenMail API server at http://0.0.0.0:8080/`，版本 `GreenMail standalone v2.1.14`，启动参数含 `-Dgreenmail.auth.disabled`。
- **GreenMail 的完整回路实测通过**：用 `smtplib` 投递原邮件与一封带 `In-Reply-To` / `References` 的回复（均成功，任意口令可 AUTH），再以**收件人账号** `reply@example.com` 登录 `imaplib.IMAP4(127.0.0.1, 3143)`：信箱被按账号自动建出、`SELECT INBOX` → `OK`、两封信原样取回（`Message-ID` 与 `In-Reply-To` / `References` 保持），并按 `HEADER In-Reply-To "<probe-1@example.com>"` **搜索命中回复**——即流③的关联做法可行。另实测：**信箱内容跨进程持久在容器里**（前一次的探针邮件仍在），但**不是持久化存储**——`docker compose down` 删掉容器后信箱即清空，所以集成测试不会受上一轮残留影响。
- **Mailpit 不支持 IMAP 的证据链**：`docker run --rm axllent/mailpit:v1.31.3 --help | grep -ci imap` → **0**；镜像 `ExposedPorts` 只有 `1025/tcp`、`8025/tcp`、`1110/tcp`（镜像 ENV 里没有任何 `MP_IMAP_*`）；`--help` 里有 `--pop3`（默认 `[::]:1110`）而无任何 IMAP 选项。上游 issue **#587「IMAP support?」与 #730「[Feat] Imap Support」均以 `state_reason: not_planned` 关闭**（#249 的 `completed` 落在 POP3 上）。

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
| `scripts/check.sh` | 门禁入口；五步定义与判据见 `docs/research/03-dependencies-and-toolchain.md` 与 `scripts/README.md` |
| `scripts/dev-up.sh` | 起开发依赖（读 `compose.dev.yaml`） |
| `compose.dev.yaml` | 开发依赖定义：PostgreSQL + GreenMail（SMTP/IMAP/POP3），只绑回环 |
| `/usr/local/bin/git` | git 2.49.1（源码编译到 `/usr/local`，覆盖默认解析；系统 `git-2.27.0` 仍在 `/usr/bin` 后备）——**在机器上，不在仓库里** |
| `/etc/docker/daemon.json` | registry mirror（**在机器上，不在仓库里**） |
| `/etc/yum.repos.d/docker-ce.repo` | docker-ce 安装源，指向 aliyun（**在机器上，不在仓库里**） |
| `.gitattributes` | 换行符强制规则（一切文本文件 LF） |
| `.pre-commit-config.yaml` | pre-commit 钩子定义（钩子已 `install`，见上文） |
| `pyproject.toml` | dev 依赖组与工具链配置；依赖清单与门禁配置的结论见 `docs/research/03-dependencies-and-toolchain.md` |
| `deploy/` | 部署脚本与交付链的落点：新链路脚本尚未实现，其形态属未定项 |
| `docs/research/01-delivery-and-deploy.md` | 生产服务器与交付链事实（本册不重复） |
| `docs/open-questions.md` | 未定项的唯一清单 |

## 未定项

详见 `docs/open-questions.md`：

- D13 基础镜像 tar 由哪台机器、用什么工具产出

**已关闭、不再列为未定项**：D3（开发机的 Docker 能力——跑得了 Docker）；D16（本地 IMAP 捕获服务——结论为改用 GreenMail，实测证据见上）；Mailpit 的 `MP_*` 变量名与 IMAP 能力（已对容器实跑核对完毕）。
