# 01 交付与部署

> **本册是「交付链与生产部署」的当前有效结论真源。**
> 只写：当前结论、当前实测证据、已否决方向、落地位置、未定项。

## 结论

**平台与前提**

- 生产目标是 **CentOS 8（实测 `CentOS Linux release 8.2.2004 (Core)`）**，系统保持不变，已知悉并接受**不再有安全更新**这一后果。
- 生产环境的前提是：**与公网隔离，但可达内网镜像源**（Nexus 类，以路径形式暴露）。
- **运行时不得依赖任何外部网络**（公网或内网源）：模型下载、遥测上报、远程配置拉取、在线许可证校验一律禁止。可达内网 yum / PyPI 源只对**构建期**成立。
- 生产宿主规格是所有者确认的**最终规格**：CentOS 8.2.2004 / 内核 4.18.0-193.el8 / 8 vCPU / 7.6 GiB 内存 / 7.9 GiB swap / `/` 可用 46G + 独立 `vdb` 256G ext4 挂 `/data` / SELinux Enforcing / firewalld active（nf_tables 后端）。
- 应用与 PostgreSQL **同机、同一个 compose** 运行：应用容器 + `postgres:17` 容器，数据落宿主机目录 `./data/postgres`。

**交付链**

- **镜像在 CentOS 8 生产服务器上本地构建**，构建与生产运行共用同一台机器，构建峰值资源占用**未实测**。**开发机也具备构建能力**（Docker 26.1.3，与生产同版本、同 image store 模式；三个基础镜像都能在它上面拉到，读数见 `docs/research/05-dev-and-build-environment.md`），**但要不要改成开发机构建，留到开发完成后再定**——这是刻意的推迟，不是遗漏。
- **平台只在 `docker build` 阶段钉死**：`docker build --platform linux/amd64`。这是唯一真正决定镜像架构的环节。
- **`docker save` / `docker load` 一律不传 `--platform`**：该参数需要 API 1.48（Engine v28），服务器 API 实测 1.45，传了会因未知参数直接失败。
- **基础镜像以镜像 tar 形式从外部带入服务器并 `docker load`**；介质传输对象由「应用镜像 + 依赖镜像」收缩为「基础镜像 tar」。
- 必须物理抵达服务器的镜像共**三个**：`python:3.13-slim-trixie`（构建期）、`ghcr.io/astral-sh/uv:0.12.20`（构建期）、`postgres:17`（运行期）。
- 构建输入（`Dockerfile`、`pyproject.toml`、`uv.lock`、`src/`、`compose.yaml`、`.env`）必须送上服务器（以及交付脚本自身——**待实现**）；宿主**没有 `git`**，只能靠 `rsync` / 归档传输。
- 交付前必须做**单平台归档自检**：tar 必须是单平台 `linux/amd64`、**不含 image index / manifest list、不含 attestation**。自检**不依赖 Docker、只用 Python 标准库**，在 tar 进入服务器之前执行。
- sha256 旁路文件格式固定为 `"<hash>  <name>"`（**两个空格**）+ **LF 换行**；CRLF 会让 `sha256sum -c` 失败。
- **podman 保留为将来可能的替代**：生产宿主（CentOS 8）自带 podman 而不带 docker，因此 podman 在目标宿主上天然可得；当前不采用，但作为备选保留。**注意开发机已改用 Docker 且不再安装 podman**（原因见 `docs/research/05-dev-and-build-environment.md`），本册说的是生产宿主。

**镜像选型**

- 生产基础层锁定 **`python:3.13-slim-trixie`**：**必须锁 codename tag，不用浮动的 `python:3.13-slim`**——它已经在一个月内从 bookworm（glibc 2.36）漂到 trixie（glibc 2.41），两次构建可能产出不同 ABI 的镜像。
- 锁 codename 的代价：基础镜像的安全更新**不会自动进入**，需要人工定期评估并重建。
- 构建阶段使用 uv 官方镜像 **`ghcr.io/astral-sh/uv:0.12.20`**，只从中取 `/uv` 与 `/uvx` 两个二进制。
- 运行镜像**保留 uv**：数据库迁移要在服务器上以 `uv run alembic` 执行，服务器上不能临时安装工具。
- 镜像架构固定 `linux/amd64`；`compose.yaml` 里的 `platform: linux/amd64` 只是**运行期保险**，不决定架构。
- **各基础镜像 tag 的 digest 会随时间漂移，写进脚本前必须重新复核**（当前观测值见「实测证据」；见「未定项」D1）。
- **预演的边界（保留的有效结论）**：预演**不能**证明镜像自身的 glibc 兼容性（容器自带 glibc，与宿主 glibc 2.28 无关）；RHEL 8 用户态 ABI 只需确认交付脚本用到的工具链与目标同代。

**服务器侧硬约束**

- `postgres:17` 的数据卷挂载宿主机目录 `./data/postgres`，首次启动前必须 `chown -R 999:999`（容器内 postgres 用户的 uid/gid）。
- 宿主 **SELinux 为 Enforcing**，因此挂载选项 `:z` / `:Z`（或等价的 `chcon` 重打标签）是**必须做的**，不是待确认项。
- firewalld 放行端口用 `firewall-cmd --permanent --add-port=<端口>/tcp`；**不得在容器运行期间执行 `firewall-cmd --reload`**——reload 会冲掉 Docker 插入的 iptables 链，可能导致容器网络中断。
- **不把 5432 暴露到宿主机**：应用与库在同一个 compose 网络里用服务名 `postgres` 互访。
- compose 需要 **v2**（`docker compose` 子命令）：编排文件不含 `version:` 字段，且用到 `depends_on.condition: service_healthy`。

**维护与回滚**

- 备份：停机后打包数据目录 `./data/postgres`；**热备必须用 `pg_dump`，不要直接拷正在写入的数据目录**。
- 回滚 = 改 `VS_EMAIL_FORWARDING_IMAGE` 成上一个版本 tag 再重启，**不需要重新构建**；若新版本含破坏性迁移，需**单独**执行 `alembic downgrade`，不能只回滚镜像。
- 镜像 tag 是版本化的：服务器上建议至少保留最近两个版本的镜像与交付 tar。

**已知风险（登记，不在本册解决）**

- 宿主配置的 yum 源版本与宿主发行版（8.2）不匹配，安装宿主包时可能造成**未授权的局部升级**（见「未定项」）。
- 基础镜像 tar「由哪台机器、用什么工具产出」**未定**（见「未定项」）。
- 服务器上那一版 Docker 处于**无官方支持状态**，且是否存在可升级路径未确认（见「未定项」）。

**待实现脚本必须保留的方法性结论（原 deploy/ 脚本的有效部分）**

- **`docker load` 之后必须逐 tag `inspect` 复检架构**：预期 tag 从 `docker load` 输出的 `Loaded image: ` 前缀枚举，任一 tag 不是 `linux/amd64` 即判定失败并以非 0 退出。
- **归档自检必须同时支持两种 tar 布局**：OCI 布局（归档内有 `index.json`）与 legacy docker-archive 布局（归档内有 `manifest.json`）。
- **校验完整性时先 `cd` 到 tar 所在目录再 `sha256sum -c --status`**；旁路文件缺失时打印实算 hash 供人工比对——这是**降级路径，不是直接失败**。
- **归档自检必须在生成 `.sha256` 旁路文件之前执行**；自检工具的退出码语义固定为 `0` 通过 / `1` 归档不合格 / `2` 用法或读取错误。
- **归档自检必须显式指定期望的基础镜像个数**（新链路下是 **3 个**）；旧的「默认 3 / 含预演按 4 计」那套参数语义已随预演环境一起作废，不得沿用。
- **迁移用 `docker compose run --rm app uv run alembic upgrade head`**：用 `run --rm` 而**不是**对跑着的容器 `exec`；**不要加 `--no-deps`**，让 compose 先把数据库等到 healthy；`migrations/` 里还没有版本时首次迁移**必然失败、属预期**，脚本必须把这条判别法打印出来。
- **`docker compose up -d` 返回 0 不等于服务可用**：启动要同时做 `docker compose up -d` 与 `/healthz` 轮询才算成功（默认 **30 次 × 2s**，可用环境变量覆盖）；从 `.env` 解析端口时要容忍 CR、引号与行尾注释；宿主没有 `curl` 时降级为在容器内用 Python 标准库探测。
- **介质传输渠道要求**：**可续传、不得改动二进制内容、文件名一律 ASCII、`.env` 单独传输**。
- **读 `docker load` 的输出用 `sed -n` 而不是 `head`**：`head` 会让 docker 收到 SIGPIPE，在 `set -o pipefail` 下变成假失败（exit 141）。

## 实测证据

- **生产宿主发行版与内核**：`CentOS Linux release 8.2.2004 (Core)`；内核 `4.18.0-193.el8.x86_64`（服务器实机诊断输出）。
- **生产宿主资源与磁盘**：`nproc` = 8 vCPU；内存 total 7.6 GiB、available 5.6 GiB；swap 7.9 GiB；`/` 为 `cl-root` 50G、**可用 46G**（xfs）；另有独立盘 `vdb` **256G**（ext4）挂 `/data`（同一次诊断输出）。
- **生产宿主安全与网络**：`getenforce` = `Enforcing`；`systemctl is-active firewalld` = `active`；`iptables --version` = `iptables v1.8.4 (nf_tables)`（同上）。
- **生产宿主可用工具**：`rsync` / `tar` / `sha256sum` / `python3` / `curl` / `wget` / `unzip` 都有，**没有 `git`**（同上）。
- **服务器 Docker 版本**（在服务器上 `docker version` 实测）：Client 与 Server Engine 均为 **26.1.3**、**API 1.45**（最低 1.24）、`linux/amd64`、Engine 构建于 **2024-05-16**；同版 **containerd 1.6.32 / runc 1.1.12**。
- **服务器 image store 模式**（在服务器上 `docker info` 实测）：`Storage Driver=overlay2` ⇒ **classic image store**（containerd store 才显示 `overlayfs`）；同一次输出另有 `CgroupVersion=1`、`Root=/var/lib/docker`；`docker images` **为空**。
- **由上述实测直接推出**：`--platform` 不可用（1.45 < 1.48）；**Engine ≥ 25 才能读 OCI 归档**（moby v25.0.0 起 `docker save` 产出 OCI 格式），26.1.3 满足；**classic image store 载不动 image index / manifest list**。
- **服务器宿主支持状态**：Docker 官方安装文档只列 CentOS Stream 9 / 10，**已不再支持 CentOS 8 作为宿主**；该 Engine 构建于 2024-05-16，容器运行时的安全补丁停在该时点。
- **内网源三类覆盖**（生产环境实测）：yum / RPM 源**可用**——`makecache` 成功、实际安装过包，覆盖 CentOS vault、EPEL 8、Rocky 9、Alma 9 等 **30+ 仓库**；PyPI 源**可用且覆盖完整**——代理公共 PyPI、真实安装过 wheel 成功；Docker 镜像**内容完整但客户端无法直连消费**——多个官方镜像的 manifest 可访问，但该源以路径形式暴露、无独立端点，不符合 registry 协议，直连报 **content-type 解析错误**（已知限制，未解决）。
- **开发机（CentOS 8 Linux 虚拟机）能跑 Docker**，版本与生产对齐（26.1.3 / overlay2 / cgroup v1），交付链需要的三个基础镜像均已实测拉取成功 ⇒ **两侧都具备构建能力**；本机侧读数见 `docs/research/05-dev-and-build-environment.md`。
- **浮动 tag 漂移**（registry 实测）：`3.13.5-slim` = Debian 12 bookworm / glibc 2.36，`3.13.6-slim` = Debian 13 trixie / glibc 2.41，tag 名一个字没变。
- **已观测 digest（写进脚本前必须重新复核）**：`ghcr.io/astral-sh/uv:0.12.20` = `sha256:100047e74f30778ab704942321a09750d6158739573ff58bf3924085cc6cd2d8`（开发机实测）。
- **`python:3.13-slim-trixie` 的当前 digest = `sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f`（开发机实测）**。锁 codename 的 tag 同样会漂，**写进交付脚本前一律重新复核**；任何册里的 digest 都不是可直接引用的现值。

## 已否决方向

- 「生产环境不接触任何网络源」这一旧前提 —— 原因：生产环境实测可达内网 yum / PyPI 源；真实约束是**运行时不得依赖任何外部网络**，把它写成「接触不到任何源」会掩盖构建期可用内网源这一事实。
- 给旧 Windows 开发机开启嵌套虚拟化 —— 原因：需改宿主 hypervisor 配置，不可控、不在项目权限内。**该机器已弃用。**
- 服务器直接从内网 Docker 源 `pull` 基础镜像 —— 原因：该源路径式暴露、无独立端点，与 registry 协议不符，直连报 content-type 解析错误（已知限制，未解决）。
- 要求服务器把 Docker 升到 ≥ 28 以支持 `--platform` —— 原因：CentOS 8 已不被 Docker 官方支持，升级路径不明，**不能把交付前提建立在没有把握的升级上**。
- 生产镜像内部用 AlmaLinux 8 自装 Python —— 原因：两套 libc ABI、维护成本高，且容器自带 glibc 已消除该问题。
- 宿主原生部署（CentOS 8 直接装 Python + PostgreSQL）—— **未采纳、也未评估完成，不是已否决的结论**，由所有者决定；其中两条子路径此前已记录过明确障碍：宿主**离线安装 PostgreSQL RPM** 需要处理 vault 源与一整套相互依赖的包；宿主**系统 Python 是 3.6.8**，无法运行本项目。
- `postgres:18` —— 原因：`PGDATA` 与 `VOLUME` 有破坏性变更，挂载点写错会把数据写进容器层。
- alpine 基础层 —— 原因：两套 libc ABI + psycopg 二进制 best-effort（**不是**「缺 musl 轮子」，cp313 上确实有 `musllinux_1_2` 轮子）。
- `centos:8` 镜像作为预演 / 基础层 —— 原因：仍可 pull 但实际是 8.4.2105、8.5.2111 等 tag 返回 404，全部 tag EOL；`rockylinux/rockylinux:8` 冻结在 8.10、`library/rockylinux:8` 停在 8.9，均不用。
- 开发机上的 `almalinux:8` 预演环境（原预演环境目录，**已删除**）—— 原因：它宣称覆盖的内容与归档自检重叠，而它**自己列为不覆盖**的部分（SELinux、firewalld、内核、cgroup v1、老版本 Docker）必须在真机上核对。**该环境已判定删除。**

## 落地位置

- `Dockerfile`：生产镜像定义——多阶段构建、锁 codename 的 Python 基础层、构建期 uv 镜像、运行镜像保留 uv、非 root（uid/gid 10001）、容器内固定监听 8000、`HEALTHCHECK` 用标准库打 `/healthz`、`CMD` 走 `uv run uvicorn`。
- `compose.yaml`：生产编排——`app` 与 `postgres:17` 两个服务、应用镜像由 `${VS_EMAIL_FORWARDING_IMAGE}` 指定（回滚改这里）、`platform: linux/amd64`（运行期保险）、数据卷 `./data/postgres:/var/lib/postgresql/data`、postgres 健康检查、自定义 bridge 网络、不映射 5432。
- `.env.example`：全部可变配置的占位，含 `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` / `VS_EMAIL_FORWARDING_IMAGE` / `HTTP_PORT`；真实 `.env` 永不进版本库。
- `docs/open-questions.md`：**未定项的唯一清单**（本册只列指针）。
- `deploy/`：**即将只留一份职责 README**，脚本与手册随之清空。以下落地位置**待实现**，判读标准与约束以本册为准：
  - 镜像构建脚本（`docker build --platform linux/amd64`，在服务器上执行；**要不要改到开发机构建留到开发完成后定**）——**待实现**。
  - 基础镜像 tar 的导出、带入与 `docker load` 流程——**待实现**。
  - 交付前单平台归档自检（不依赖 Docker、只用 Python 标准库）——**待实现**；原归档自检脚本（**已随 `deploy/` 一起删除**），其判据已记于本册「结论」，须在待实现的脚本里重建。
  - sha256 旁路文件的生成与校验（两个空格 + LF）——**待实现**。
  - 迁移与启动脚本（容器内 `alembic upgrade head`、`docker compose up -d` + `/healthz` 轮询后才算成功）——**待实现**。

## 未定项

详见 `docs/open-questions.md`：

- D1 各基础镜像 tag 的 digest 需在写进脚本前复核
- D2 部署脚本的具体形态（新链路下的构建 / 打包 / 载入 / 启动 / 迁移脚本均**尚未实现**）
- D4 备份与日志采集、保留期
- D6 本地邮件捕获工具（Mailpit）是否采用
- D8 服务器侧 Docker 是否有可升级路径
- D12 宿主 yum 源版本与发行版错配的风险
- D13 基础镜像 tar 由哪台机器、用什么工具产出（与「镜像在哪一侧构建」同题，留到开发完成后再定）
- D14 产出方 CLI ≥ 28 时 `docker save` 是否可放宽使用 `--platform`（现行结论仍是「一律不传」）
- 镜像构建改到开发机的可能性 —— **留到开发完成后再定**（本册不列为当前待决项，以免交付链在开发期反复改写）
