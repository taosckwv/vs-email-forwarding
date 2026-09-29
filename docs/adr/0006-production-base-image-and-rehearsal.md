# 0006. 生产基础镜像选型与 CentOS 8 部署预演

- 状态：已接受
- 日期：2026-09-29
- 决策者：项目所有者
- 依据：基础设施核查报告（2026-09-29）；文中 digest、构建号、版本漂移均为该报告的实测结果

## 背景

生产宿主是 CentOS 8，其 glibc 为 **2.28**（`glibc-2.28-164.el8`）。这原本被列为重大风险：大量项目的最新 wheel 是在较新发行版上构建的 `manylinux_2_34` 标签，在 glibc 2.28 上无法安装，`pip` 会退化为源码编译，而源码编译又需要一整套 `-devel` 包。

但所有者最终选择了「生产以 Docker 容器运行应用」，这使上述风险的适用范围发生了根本变化，需要明确记录这个关键区分。

## 决策

### 关键区分（本 ADR 的核心）

> **宿主机是 CentOS 8，与容器内的发行版，是两件相互独立的事情。**
> 容器共享宿主内核（CentOS 8 的内核为 4.18），但**用户态（glibc、包管理器、Python）完全由镜像决定**。

因此：**CentOS 8 的 glibc 2.28 问题在生产容器内部不存在**，只要不主动选择让镜像内部也是 CentOS 8。生产镜像内部是 Debian，其 glibc 版本远高于 2.28，所有需要的 wheel 都可直接安装。

### 1. 生产镜像基础层：`python:3.13-slim-trixie`

**必须锁 codename tag（或 digest），不使用浮动的 `python:3.13-slim`。**

依据（实测）：`3.13-slim` 是浮动 tag，已经在一个月内从 **Debian 12 bookworm**（glibc 2.36，镜像内 Python 为 `3.13.5-slim`）漂移到 **Debian 13 trixie**（glibc 2.41，`3.13.6-slim`）。本项目生产**完全离线、镜像靠 `docker save` / `load` 交付**，浮动 tag 意味着两次构建会产出**不同 ABI 的镜像**，而且服务器上无法联网回退重建——一旦出问题只能重新走介质传输。因此必须锁死 codename。

**明确不使用 alpine 变体。** 理由**不是**「没有 musl 轮子」——实测 `psycopg-binary`、`asyncpg`、`greenlet` 在 cp313 上**确实都有 `musllinux_1_2_x86_64` 轮子**。真正的理由有两条：

1. musl 与生产宿主 CentOS 8 的 glibc 2.28 属于**两套 libc ABI**，容器与宿主之间会多出一层不可控变量；
2. `psycopg` 官方明确声明其二进制包是尽力而为（原文：*Binary packages are produced on a best-effort basis*），不适合作为离线、无回退手段的生产环境基础。

**已观测到的 digest（待复核）**：`python:3.13-slim-trixie` 的 amd64 digest 当前为 `sha256:37134a49d21d2120e4c4d73bb76f8a4ab9aef31f096f7ec2ead48c2feead4332`，与此刻的浮动 tag `python:3.13-slim` **相同**——这正说明 trixie 是 `slim` 当前指向的目标，也因此必须锁 codename：它随时会再漂。**写入脚本前请重新复核。**

构建阶段使用的 uv 基础镜像定为 **`ghcr.io/astral-sh/uv:0.12.20`**（已用 registry manifest 请求验证存在，digest `sha256:100047e7…`）。

### 2. 预演容器：`almalinux:8`（仅用于离线部署预演，不用于生产运行）

预演的**验证目标**是：

- `docker load` 镜像 tar 能否成功；
- `docker run` 能否在 CentOS 8 宿主上正常启动（内核 4.18、默认 cgroup v1 的兼容性）；
- Alembic 迁移能否在容器内跑通；
- 应用能否连接到同机的 PostgreSQL 容器；
- 服务启停与日志输出是否符合预期。

镜像选择：

| 候选 | 结论 |
|---|---|
| **`almalinux:8`** | **首选。** 当前仍活跃维护，最近构建 `8.10-20260902`，glibc `2.28-251.el8_10`，dnf 源开箱可用（repo `repomd.xml` 返回 200）。amd64 digest `sha256:158fba66c3434c58d07fb48cb6f19e3da84a7d9494cb07774d5d36a746136dce`（**digest 需复核**，写入脚本前请重新确认）。 |
| `rockylinux/rockylinux:8` | 次选。镜像本体冻结在 8.10（2024-06-05），但发行版受支持至 2029-05-31，repo 在线。 |
| `library/rockylinux:8` | **不要用。** 停留在 8.9.20231119（2024-05-30），其官方 README 自述存在技术限制，并建议改用 `rockylinux/rockylinux`。 |
| `quay.io/centos/centos:stream8` | **不要用。** 冻结于 2024-06-03，默认源已失效（`mirror.stream.centos.org` 只剩 9-stream 与 10-stream）。 |
| `centos:8` | **不作为主选**，详见下方说明。 |

**为什么不用 `centos:8`**（此处纠正了一处先前的错误记录）：

- `centos:8` **仍然可以 pull**（registry manifest 返回 200，digest `sha256:a27fd8080b517143cbbbab9dfb7c8571c40d67d534bbdee55bd6c473f432b177`）。**此前记录的「原版 `centos:8` 已从 Docker Hub 下架」是错的，已删除。**
- 但它**实际是 8.4.2105**：其 per-arch digest 与 `centos:8.4.2105` 完全相同；而 `centos:8.5.2111` 在 Docker Hub 上**根本不存在**（tag API 与 registry 均返回 404）。也就是说，它是个比预期更旧的快照。
- `library/centos` 的**全部 tag 均已 EOL**，Docker Hub 页面标注 "No supported tags"；`mirror.stream.centos.org` 已不再提供 8-stream；只能依赖 vault，而 **vault 明确不提供安全更新**。

**ABI 兼容的依据**（此前只有断言，现补出处）：AlmaLinux 8 与 Rocky Linux 8 同 CentOS 8 一样是 **RHEL 8 的重建**，三者 glibc 均为 **2.28**，且 glibc 的 NVR 属同一家族（`2.28-251.el8_10.*`），说明跟踪的是同一份 RHEL 8 源码。官方口径：Rocky Linux 官网自述 *100% bug-for-bug compatible with Red Hat Enterprise Linux*，AlmaLinux 官网 / wiki 自述 *binary compatible with RHEL*。因此**三者 ABI 相同**，用 AlmaLinux 8 预演 CentOS 8 是有效的。

### 3. 交付链的平台与版本约束

- `docker build` / `save` / `load` / compose **一律显式带上 `--platform linux/amd64`**。
- moby **v25.0.0** 起 `docker save` 产出 **OCI 归档**；**Docker Desktop 与 Engine ≥ 29 默认使用 containerd image store**，而 **classic image store 不支持 image index / manifest list**，无法加载多平台镜像；`--platform` 参数需要 **Engine v28**。
- 因此**部署前必须在服务器上执行 `docker version`**，核对 **Engine ≥ 25 且 CLI ≥ 28**，并确认 image store 模式。
- **已记录的风险**：Docker 官方**已不再支持 CentOS 8 作为宿主**（官方安装文档只列出 CentOS Stream 9 / 10），服务器上那一版 Docker 处于**无官方支持状态**——遇到容器运行时的疑难问题时没有上游支持渠道。

### 4. 同机 PostgreSQL 容器：`postgres:17`

- **选定 `postgres:17`**，数据卷挂载点**沿用** `/var/lib/postgresql/data`。
- **被否决的 `postgres:18`**：它有**破坏性变更**——`PGDATA` 改为 `/var/lib/postgresql/18/docker`，且 `VOLUME` 从 `/var/lib/postgresql/data` **移到** `/var/lib/postgresql`。选 18 意味着挂载点与目录权限都要跟着改，而**挂载点写错的后果极其隐蔽**：数据看起来已经持久化，实际写在容器可写层，一旦重建容器即全部丢失。骨架阶段不值得为此承担风险。
- **运维要求**：宿主机上的数据目录必须 `chown -R 999:999`（容器内 `postgres` 用户的 uid/gid）。
- **待确认**：SELinux 环境下可能还需要为挂载点加 `:z` / `:Z` 选项；**该细节尚未取得官方原文依据**，因此在 `deploy/` 脚本落笔前必须确认（同时记录在本文「待定项」中）。

## 后果

### 正面

- 使用官方维护的 Debian 系基础镜像，Python 与依赖的 wheel 可用性最好，无需在镜像内自行编译解释器。
- 锁定 codename tag 后镜像构建结果**可复现**：在离线交付场景下这是硬需求。
- 预演容器改用**仍在维护**的 `almalinux:8`，避免了在 EOL 且源已失效的 `centos:8` / `stream8` 上浪费调试时间。
- glibc 风险的适用面被压缩到「预演容器」这一非生产路径上。
- 预演能在真正上线前暴露离线交付流程中的问题（传输、加载、迁移、连库），而不是等到生产上第一次尝试才发现。

### 负面 / 代价

- 开发（Windows + uv）、预演（AlmaLinux 8）、生产（Debian trixie 容器）三者并存，仍然存在环境偏差；必须靠人工预演兜底（本项目不跑 CI，见 ADR-0010，因此预演只能人工执行）。
- 预演环境与实际生产仍是不同发行版，它验证的是**宿主层的兼容性与交付流程**，不能替代生产环境的验证。
- 锁定 codename tag 意味着基础镜像的安全更新不会自动进入，需要人工定期评估并重建镜像。
- **宿主 Docker 无官方支持**（CentOS 8 已从 Docker 支持列表移除），故障时只能自行排查。
- `docker save` 的 OCI 归档格式与 image store 模式的组合存在加载失败的可能，因此必须先核对服务器 Docker 版本。

## 待定项

- **uv 基础镜像 tag 已定**：`ghcr.io/astral-sh/uv:0.12.20`（已用 registry manifest 请求验证存在）。仅剩「写进 `Dockerfile` / `deploy/` 脚本前复核 digest」这一步。
- **各镜像 tag 的 digest 复核**：`python:3.13-slim-trixie`、`almalinux:8`、`ghcr.io/astral-sh/uv:0.12.20`——浮动 tag 与 digest 都会随时间漂移。
- **SELinux 环境下的数据卷挂载选项**（是否需为 postgres 数据目录加 `:z` / `:Z`）尚未取得官方原文依据，标注为待确认。

## 备选方案与被否决的原因

| 方案 | 否决原因 |
|---|---|
| 生产镜像内部也用 AlmaLinux 8 / Rocky 8（再自装 Python 3.13） | 环境一致性最高（开发、预演、生产同源），但镜像更大、需要自行安装 Python 3.13，且部分 wheel 可能装不上而需要降版本或源码编译。所有者选择了「生产用 Debian 系 slim 镜像，CentOS 8 只做预演」。 |
| 生产镜像用浮动 tag `python:3.13-slim` | 实测一个月内即从 bookworm/glibc 2.36 漂到 trixie/glibc 2.41，离线交付下不可接受。 |
| 生产镜像用 alpine 变体 | 不是因为没有 musl 轮子（cp313 上确实有），而是 libc ABI 与宿主不一致，且 psycopg 官方声明二进制包为 best-effort。 |
| 生产镜像内部用 `centos:8` | 仍可拉取，但实为 8.4.2105（8.5.2111 不存在）、全部 tag 已 EOL、只能依赖不提供安全更新的 vault。 |
| 预演容器用 `library/rockylinux:8` | 停更于 8.9.20231119，其官方 README 自述有技术限制并建议改用 `rockylinux/rockylinux`。 |
| 预演容器用 `quay.io/centos/centos:stream8` | 冻结于 2024-06-03，默认源已失效。 |
| 同机 PostgreSQL 用 `postgres:18` | `PGDATA` 改为 `/var/lib/postgresql/18/docker`、`VOLUME` 从 `/var/lib/postgresql/data` **移到** `/var/lib/postgresql`，属破坏性变更，挂载点与权限都要跟着改；挂载点写错会导致数据实际写在容器层、重建即丢失。骨架阶段不值得承担该风险，故选 17。 |
| 不做 CentOS 8 预演（Docker 只用来起 PostgreSQL 与跑测试） | 最简单，但离线部署流程要到服务器上才第一次被验证，失败成本最高。 |

## 相关

- [0005](0005-offline-docker-deployment-on-centos8.md)：离线交付流程本身
- [0001](0001-technology-stack.md)：镜像内同样固定 Python 3.13
- [0010](0010-no-ci-local-gates.md)：没有 CI 意味着兼容性预演只能人工执行
- `docs/open-questions.md` D1（镜像 digest 复核）、D7（服务器 Docker 版本，高优先级）
