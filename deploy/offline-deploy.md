# 离线交付方案说明（设计理由与待定项）

本文件回答「**为什么这样做**」；「**怎么做**」在 `deploy/README.md`。
两份文件分工固定，不要互相复制内容：操作步骤只写在 README，决策理由只写在这里。

---

## 1. 交付形态：为什么是「离线镜像 tar」，不是离线 RPM，也不是离线 wheels

候选方案与结论：

| 方案 | 为什么不选 |
|---|---|
| **服务器上装 Python + 离线 wheels（`pip install` 一堆 whl）** | 要在服务器上维护解释器、虚拟环境、系统库（libpq、openssl 之类），还要处理「这台机器能装、那台装不上」的编译依赖。而且依赖树是**运行时才展开**的：装到一半失败时现场没有网可以重试。 |
| **打成 RPM 交付** | 需要维护 spec、构建机与目标机的 RPM 宏与版本要对齐，PostgreSQL 与 Python 依赖还得走系统包管理器；离线 RPM 的依赖闭包要用 `repotrack` 之类工具凑齐，且 RPM 与容器两套运行时并存会让排查口径分裂（到底是 RPM 的问题还是容器的问题？）。 |
| **内网私有 registry** | 需要先在服务器上搭建 registry，或者打通一条到开发机的网络通道——而真实前提是**服务器不连公网**，项目也不想为此引入额外基础设施。 |
| **离线镜像 tar（本项目选择）** | 交付物是**自包含的**：开发机能跑起来的那一份，原样搬到服务器。运行时不依赖宿主机的 Python、系统库、网络。校验就是一次 sha256。回滚就是换一个 tag。代价是文件大（数百 MB~1GB 量级），以及要显式管理镜像平台与 Docker 版本（见 §5）。 |

配套决定：

- **基础镜像必须和应用镜像打进同一个 tar**。离线服务器拉不到 `postgres:17`，
  `docker compose up -d` 一旦发现本地缺镜像就会尝试联网并失败。
- **`compose.yaml` 不写 `build`**，只写 `image`。这样即使误操作也不会在服务器上
  触发构建（服务器没有 uv 的构建环境，也没有网）。

---

## 2. 为什么生产基础层用 `python:3.13-slim-trixie`（Debian），而不是 CentOS 8 基础层

「生产宿主是 CentOS 8，那基础镜像也该用 CentOS 8」听起来对称，但会引入一堆问题：

1. **CentOS 8 上游已 EOL**。系统包（含 OpenSSL 等）只能从 vault 取，且 vault
   **明确不提供安全更新**。把生产容器的基础层建在一个拿不到安全补丁的发行版上，
   等于把风险固化进交付物。
2. **`centos:8` 这个 tag 有误导性**。它**仍然可以 pull**（所以不要说"已从 Docker Hub
   下架"——那是错的），但它实际等于 **8.4.2105**：既不是最后的 CentOS 8 版本，
   `centos:8.5.2111` 在 Docker Hub 上根本不存在（404）。而且 `library/centos` 的
   全部 tag 都已标记 EOL / DEPRECATED，`mirror.stream.centos.org` 也没有 8-stream 了，
   只剩 vault。拿它当基础层，得到的是一个"看起来是 CentOS 8、实际是 8.4"的环境。
3. **容器不需要与宿主同发行版**。容器共享宿主内核，自带用户态；glibc 由镜像提供，
   不由 CentOS 8 提供。用官方 `python` 镜像可以拿到长期维护、cve 修复及时、
   与 uv 官方文档路径一致的构建体验。

### 为什么是 codename tag，不是裸 `slim`

`python:3.13-slim` 是**浮动 tag**，指向哪个 Debian 发行版没有任何提示：

| 时间 | tag | 实际基础系统 | glibc |
|---|---|---|---|
| 2025-07-23 | `3.13.5-slim` | Debian 12 bookworm | 2.36 |
| 2025-08-14 | `3.13.6-slim` | Debian 13 trixie | 2.41 |

tag 名字一个字没变，glibc 却跨了大版本。对本项目尤其致命：**开发机构建 → save →
离线服务器 load**，服务器无法重新构建、也无法回退，两次构建产出不同 ABI 时现场
根本判断不出差异从哪来。所以 codename 必须写死（当前取 `trixie`）。
保守备选是 `python:3.13-slim-bookworm`（Debian 12 / glibc 2.36），如果担心 trixie
上某些二进制轮子覆盖不全，它是更稳的选择——但那要有意识地选，并写进 ADR。

### 为什么不用 alpine

要澄清一个**不成立**的理由：不是"alpine 上没有 musl 轮子"。已核实
`psycopg-binary` / `asyncpg` / `greenlet` 在 `python:3.13-alpine` 上**确实有**
cp313 `musllinux_1_2` x86_64 轮子。

真实的理由有两条：

1. **musl 与 glibc 是两套 libc ABI**。生产宿主是 CentOS 8（glibc 2.28），
   开发/构建用 musl，一旦出现与 libc 相关的诡异行为（DNS 解析、locale、线程栈大小、
   `getaddrinfo` 差异），排查面直接翻倍，而现场没有网可以试验。
2. **psycopg 官方明确说二进制包是 best-effort**：
   *"Binary packages are produced on a best-effort basis"*。
   离线环境里一旦某个版本恰好没有对应轮子，就是**装不上**（服务器无网、无法现场编译）。
   glibc 轮子的覆盖面显著更广，选择它是在降低离线交付的风险。

---

## 3. 为什么运行镜像里保留 uv

看起来"运行镜像只需要 Python"，但保留 uv 有三个具体理由：

1. **迁移要在服务器上跑**：`deploy/migrate.sh` 执行的是
   `docker compose run --rm app uv run alembic upgrade head`。服务器上不能临时安装
   工具（无网），所以 uv 必须已经在镜像里。
2. **`uv run` 保证用的是构建期同步好的 `.venv`**：运行期不会重新解析依赖
   （镜像里还设了 `UV_FROZEN=1`，禁止 uv 改动 lock），离线环境下不会因为
   "想去更新元数据"而访问网络。
3. **体积代价很小**：只拷 `/uv` 与 `/uvx` 两个二进制，不带构建期缓存。

配套的构建期决定（详见 `Dockerfile` 注释）：多阶段构建（运行层不带编译产物与缓存）、
`--frozen`（严格按 lock 安装，不就地改写）、非 root（uid 10001）运行。

---

## 4. 为什么用 almalinux:8 做 CentOS 8 离线部署预演

预演环境在 `deploy/centos8-rehearsal/`。

**首选 `almalinux:8`**：与 RHEL 8 / CentOS 8 同 ABI（glibc 2.28），
且**仍在活跃维护**（最近构建 `8.10-20260902`，glibc `2.28-251.el8_10`，
dnf 源开箱可用）。
备选 `rockylinux/rockylinux:8`：镜像冻结在 8.10（2024-06-05），但发行版支持到
2029-05-31，repo 在线。

**不要用 `library/rockylinux:8`**：它停在 `8.9.20231119`（2024-05-30），
官方 README 已明确让改用 `rockylinux/rockylinux`。

**不要用 `centos:8`**：理由见 §2.2（仍可 pull，但等于 8.4.2105，且全部 tag EOL、
只剩 vault、无安全更新）——预演环境用一个已经拿不到安全更新的镜像没有意义。

预演的**目的不是跑测试**（这一点在 `centos8-rehearsal/README.md` 里反复强调：
单元与集成测试在开发机跑，见 `scripts/check.ps1` / `check.sh`），而是：

- 提前暴露"交付脚本在 RHEL 8 用户态下跑不跑得通"（bash/coreutils/tar/sha256sum/dnf 行为）；
- 提前暴露 SELinux、cgroup、docker/compose 版本这类环境差异；
- 让 load / migrate / run 这套流程在接近现场的 shell 里先走一遍。

**必须承认的局限**：预演**不能**验证内核行为。预演容器跑在开发机的
Docker Desktop 上（WSL2 内核 6.6），而目标宿主 CentOS 8 是 4.18 —— cgroup v1/v2、
iptables/nftables、以及某些 sysctl 行为都不同。真正的 ABI/内核级验收只能在
**真实 CentOS 8 机器**上完成。

---

## 5. `docker save` / `docker load` 的平台与版本约束（现场最容易踩）

1. **三处平台必须统一写 `linux/amd64`**：
   - `deploy/build-image.ps1 -Platform linux/amd64`（`docker build --platform`）
   - `compose.yaml` / `compose.dev.yaml` 里的 `platform: linux/amd64`
   - `deploy/save-image.ps1` 与 `deploy/load-image.sh` 的 `--platform linux/amd64`

   统一的目的：保证「开发机存的」就是「服务器跑的」，并且将来开发机换成 arm64
   （Apple Silicon）时不会**静默**产出错架构镜像——那种错误在服务器上表现为
   `exec format error`，排查起来很费时间。

2. **`docker save` 的产物格式变了**：自 moby **v25.0.0** 起，`docker save` 产出
   **OCI 归档**。老引擎/老工具链对 OCI 归档的兼容性不如旧的 docker-archive。

3. **`--platform` 参数要 Engine v28 才有**。低于 v28 时 `load-image.sh` 会自动
   退化为不带该参数，并提示人工核对镜像架构。

4. **containerd image store 的差异**：Docker Desktop 与 Engine ≥ 29 默认使用
   containerd image store，而 **classic image store 不支持 image index /
   manifest list**，无法 load 多平台镜像。服务器上很可能是老引擎 + classic store。

5. 因此部署前**必须**核对服务器版本（`docker version`）：**Engine ≥ 25 且 CLI ≥ 28**。
   若服务器是老版本（CentOS 8 上很可能是 20.10 / 24 / 26），load 可能失败，
   需要先升级 Engine，或改用其他交付方式。

6. **风险记录（不是本项目能解决的）**：Docker 官方**已不再支持 CentOS 8 作为宿主**
   （官方安装文档现在只列 CentOS Stream 9/10）。服务器上那版 Docker 处于
   **无官方支持**状态，出问题只能靠社区/源码。这条要写进交付说明，别让运维以为
   "有官方兜底"。

---

## 6. 被否决/搁置的备选方案（汇总）

| 决策点 | 选择 | 被否决的备选 | 关键理由 |
|---|---|---|---|
| 交付形态 | 离线镜像 tar | 离线 RPM、离线 wheels、内网 registry | 自包含、可校验、可回滚；不引入额外基础设施 |
| 生产基础层 | `python:3.13-slim-trixie` | `centos:8` 基础层 | CentOS 8 已 EOL、只剩 vault、无安全更新；容器用户态与宿主发行版无需一致 |
| Debian 变体 | trixie（备选 bookworm） | 裸 `slim`、`alpine` | 浮动 tag 会在无提示的情况下换 glibc；musl 与 glibc 两套 ABI，且 psycopg 二进制包是 best-effort |
| 数据库 | 与应用同机 `postgres:17` 容器 + 宿主机目录卷 | 宿主机装 PostgreSQL、托管数据库 | 一期单机部署，不引入额外运维面；同 compose 管理便于一起备份/回滚 |
| 数据目录 | 宿主机目录 `./data/postgres`（postgres:17 的 `/var/lib/postgresql/data`） | 命名卷；`postgres:18`（挂载点改为 `/var/lib/postgresql`，`PGDATA` 也变了） | 备份=打包目录；18 的挂载点变更是破坏性的，一期不追新 |
| 预演镜像 | `almalinux:8` | `centos:8`、`library/rockylinux:8`、（次选 `rockylinux/rockylinux:8`） | 前者 EOL 且实为 8.4；后者停在 8.9；almalinux:8 活跃维护且 glibc 2.28 |
| 运行期工具 | 镜像内保留 uv | 运行镜像不带 uv、靠宿主机跑迁移 | 服务器无网不能装工具；迁移必须在目标环境执行 |
| CI | 不跑（本地门禁） | GitHub Actions 等 | 见 ADR-0010；门禁落在 `scripts/check.*` |

---

## 7. 尚未确定的事项（不要当成已定）

| # | 待定项 | 影响 | 谁来定 |
|---|---|---|---|
| 1 | **服务器 IP、SSH 端口、部署目录** | `deploy/README.md` 的传输章节只能写占位；现场步骤里的绝对路径都可能变 | 所有者 |
| 2 | **传输介质 / 跳板机通道** | 决定了校验与传输脚本要不要写成自动化；目前只要求"能续传 + 传 sha256" | 所有者 |
| 3 | **镜像确切 digest**（`python:3.13-slim-trixie` / `ghcr.io/astral-sh/uv` / `postgres:17` / `almalinux:8`） | 定版时应把 tag 换成 `tag@sha256:...` 才能真正锁死内容。当前只在注释里记录"抓取当日"的 digest，需要复核 | 交付定版时 |
| 4 | **服务器 Docker 版本** | 决定 `--platform` 能不能用、OCI 归档能不能 load（见 §5） | 现场核对，结果回填 `deploy/README.md` |
| 5 | **SELinux 下数据目录的正确处理方式**（`:z`/`:Z` 挂载选项 vs `chcon`） | 挂载点能否被 Postgres 写入；未取得官方原文确认，**标注为待确认** | 现场实测后回填 |
| 6 | **Alembic 初始化时点** | 决定 `deploy/migrate.sh` 什么时候从"必然失败"变成"必须成功" | 数据模型规格（A1–A5）落地后 |
| 7 | **`postgres:18` 升级路径** | 18 的 `PGDATA` / 挂载点变更需要一次有计划的迁移（`pg_upgrade`），不是换个 tag 就行 | 将来另开 ADR |
| 8 | **Mailpit 是否最终采用** | 影响 `compose.dev.yaml` 的 SMTP/IMAP 指向与集成测试写法 | 见 `docs/open-questions.md` D6 |
| 9 | **对外端口（`HTTP_PORT`）与是否需要对外放行** | 影响 firewalld 步骤与健康检查地址 | 所有者 |
| 10 | **`.env.example` 需要补 4 个键**：`POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB`（官方 postgres 镜像 initdb 用，且必须与 `DATABASE_URL` 一致）与 `MAIL_RELAY_IMAGE`（离线指定版本 tag）；同时生产 `.env` 里 `DATABASE_URL` 的主机要改成 compose 服务名 `postgres`。这 4 个键当前在 `.env.example` 里**都不存在**，不改的话 `compose.yaml` 直接起不来 | 直接决定部署能否成功 | 根配置维护者（`deploy/` 无权改 `.env.example`） |

注：`docs/README.md` 的写作规范要求"不写没验证过的具体值"。本目录里出现的 tag
与 digest 都标注了来源或"待复核"，没有编造。

---

## 8. 与 ADR 的关系

本方案涉及若干**不可逆或改起来很贵**的决策（基础镜像、交付方式、离线策略、
数据库形态），按 `AGENTS.md` §6 必须有 ADR：

- `docs/adr/0005-offline-docker-deployment-on-centos8.md`
- `docs/adr/0006-production-base-image-and-rehearsal.md`

本文件是**理由的展开**，不是 ADR 的替代品。若实施过程中做了与上述不同的选择
（例如改用 bookworm 基础层、或把预演镜像换成 `rockylinux/rockylinux:8`），
应新增 ADR 记录，而不是只改这个文件。
