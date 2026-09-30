# 0013. 交付形态变更：在生产服务器上本地构建 + 基础镜像以 tar 带入

- 状态：已接受
- 日期：2026-09-30
- 决策者：项目所有者
- 依据：生产环境实测（内网镜像源三类覆盖 + 宿主软硬件规格，见 `docs/open-questions.md` D9 / D11）；开发机无法安装 Docker（见 `docs/open-questions.md` D3）。本文只记结论与证据类型，**不含任何内网地址、主机名、端口、账号**。

## 背景

### 前提被推翻：「生产完全离线」不成立

自 ADR-0005 起，本项目一直以「**生产完全离线**」为前提（该表述同时出现在 `AGENTS.md` §8.5 与 ADR-0005 的标题/决策中）。生产环境实测后确认：**这个前提不成立**。真实形态是——

> **与公网隔离，但可达内网镜像源**（Nexus 类，以路径形式暴露）。

内网源三类覆盖的实测结论（详见 `docs/open-questions.md` D11）：

| 类别 | 实测结论 |
|---|---|
| yum / RPM 源 | **可用**。`makecache` 成功，实际安装过包；覆盖 CentOS vault、EPEL 8、Rocky 9、Alma 9 等 30+ 仓库。 |
| PyPI 源 | **可用且覆盖完整**。代理公共 PyPI，真实安装过 wheel 成功 ⇒ **构建时能装 wheel**。 |
| Docker 镜像内容 | 源里**镜像内容完整**（多个官方镜像的 manifest 可访问），**但 Docker 客户端当前无法直连消费**：该源以路径形式暴露、没有独立端点，与 Docker registry 协议要求不符，直连报 **content-type 解析错误**。**该问题尚未解决，本 ADR 只登记为已知限制。** |

由此得到两条对决策至关重要的事实：

1. **宿主机装包（yum）与构建期装 wheel（PyPI）在服务器本地可行**——这是「改在服务器上构建」能够成立的关键；
2. **Docker 镜像内容仍无法从该源直接拉取**——所以**基础镜像依然必须由外部带入**，只是带入的对象从「应用镜像 + 依赖镜像」收缩为「基础镜像 tar」。

### 开发机装不了 Docker

开发机是**未暴露嵌套虚拟化的虚拟机**（Windows 10 22H2；`HypervisorPresent=True`，但 `VirtualizationFirmwareEnabled` / SLAT / `VMMonitorModeExtensions` 均为 False）。Docker Desktop 的 **WSL2 与 Hyper-V 两个后端都依赖硬件虚拟化与 SLAT，因此在开发机上不可用**——这不是「尚未安装、装一下就行」，而是「**这台机器装不了**」。详见 `docs/open-questions.md` D3。

这条事实直接抽掉了 ADR-0005 / ADR-0006 交付链的第一步：旧链的起点是「**开发机 `docker build`**」，而在当前开发机上这一步**根本无法执行**。

### 生产宿主的实测规格与工具（用于判断本地构建是否可行）

服务器实测为 **CentOS 8.2.2004**、内核 `4.18.0-193.el8`、**8 vCPU / 7.6 GiB 内存 / 7.9 GiB swap**（**所有者已确认这就是最终生产规格**）、`/` 可用 46G、另有独立 `vdb` 256G ext4 挂 `/data`、SELinux `Enforcing`、firewalld `active`、iptables 为 nf_tables 后端、Docker 26.1.3 / API 1.45 / Storage Driver=overlay2（classic image store）/ cgroup v1、本地镜像为空；宿主有 `rsync` / `tar` / `sha256sum` / `python3` / `curl` / `wget` / `unzip`，但**没有 `git`**；`sr0` 挂着 CentOS 8.2 安装 DVD ISO。完整事实见 `docs/open-questions.md` D9。

## 决策

1. **镜像在 CentOS 8 生产服务器上本地构建**，不再由开发机构建。
2. **基础镜像以镜像 tar 形式从外部带入服务器**并 `docker load`。至于「外部机器」具体是哪一台、用哪种工具产出这个 tar，**属实现细节，本次不决定、不展开**。
3. **环境前提的表述更正为**：

   > **与公网隔离，但可达内网镜像源；运行时不得依赖任何外部网络。**

   **只改前提表述，不改实质约束**：运行时仍然禁止任何需要访问外部网络（公网或内网源）的依赖与行为（见 `AGENTS.md` §8.5，已按本 ADR 更新）。

> 文档中一律使用占位符（如 `<internal-nexus-host>`）。**仓库内不出现任何内网地址、主机名、端口、账号**（`AGENTS.md` §8.4 / §10）。

## 与 ADR-0006 的关系（取代与仍然有效的部分）

**ADR-0006 是历史依据，正文保留、不改写。** 本 ADR 只声明取代与修订关系：

| ADR-0006 的内容 | 在本 ADR 下的状态 |
|---|---|
| **交付链「开发机 `docker build` → `docker save` → 服务器 `docker load`」** | **被取代**：改为「生产服务器上本地构建 + 基础镜像以 tar 带入」。 |
| §决策 3「平台在构建期钉死：`docker build --platform linux/amd64`」 | **仍然有效**，只是执行位置由开发机改为生产服务器。 |
| §决策 3「`docker save` / `docker load` 不传 `--platform`」（需 API 1.48，服务器 API 为 1.45） | **仍然有效**，理由未变。 |
| §决策 3「交付物必须是单平台 amd64 归档 + 交付前归档自检」 | **仍然有效**，并**明确适用于新的基础镜像 tar 路径**：`deploy/check-image-archive.py` 这类自检做法在这条路径上依然适用。 |
| §决策 3「Engine ≥ 25 才能读 OCI 归档」「classic image store 不支持 image index / manifest list」 | **仍然有效**。 |
| §决策 3「image store 模式仍待现场确认」 | **已由实测关闭**：`Storage Driver=overlay2` ⇒ classic image store（见 `docs/open-questions.md` D7），不再是待确认项。 |
| §决策 3 及备选方案中「要求服务器升级 Docker ≥ 28」被否决的理由 | **仍然有效**，本 ADR 沿用该否决理由，不重启该方案。 |
| §决策 1 基础镜像**选型**（`python:3.13-slim-trixie`、锁 codename、不用 alpine、构建阶段 uv 镜像 `ghcr.io/astral-sh/uv:0.12.20`） | **未被本 ADR 修订**：本 ADR 只改「在哪里构建」与「基础镜像如何进入服务器」，**没有改选哪一版基础镜像**。其理由（glibc / ABI、浮动 tag 漂移、vault 不提供安全更新）仍然成立；digest 复核仍按 `docs/open-questions.md` D1 执行。 |
| §决策 2 预演容器 `almalinux:8` 的**选型与理由** | **未被本 ADR 修订**。但「预演」这一环节的定位需要重新评估：旧预演验证的是「开发机构建产物经介质带入 CentOS 8 宿主」，而新形态下构建与运行同在目标宿主上，`deploy/centos8-rehearsal/` 是否仍需保留、验证目标如何调整，**属待确认，本 ADR 不决定**。 |
| §决策 4 同机 `postgres:17`、数据卷挂载点、uid 999、SELinux 挂载选项待确认 | **未被本 ADR 修订**。 |
| §待定项「各镜像 tag 的 digest 复核」 | **仍然有效**，且在新形态下同样必须做。 |

## 仍然有效的既有做法（新旧路径都适用）

1. **平台只在构建期钉死**：`docker build --platform linux/amd64` 是唯一真正决定镜像架构的环节——现在这条命令在生产服务器上执行。
2. **`docker save` / `docker load` 一律不传 `--platform`**：该参数需要 API 1.48（Engine v28），服务器 API 实测为 1.45，传了会因未知参数直接失败。
3. **交付前对 tar 做单平台自检**（`deploy/check-image-archive.py`）：**在基础镜像 tar 这条新路径上依然适用**——无论 tar 由哪台机器、用什么工具产出，进入服务器前都应先确认它是单平台 `linux/amd64`、不含 image index / manifest list、不含 attestation。

## 未决部分（本 ADR 不决定）

- **内网 Docker 镜像内容源当前无法被 Docker 客户端直连消费**：源以路径形式暴露、无独立端点，与 Docker registry 协议要求不符，直连报 content-type 解析错误。**已知限制，本次不要求解决。**
- **基础镜像 tar 由哪台机器、用什么工具产出**：属实现细节，**未定**。
- **预演环境（`deploy/centos8-rehearsal/`）在新形态下的去留与验证目标**：**待确认**。
- **是否放弃容器、改为宿主原生部署**：**未采纳、也未评估完成**，由所有者决定（见备选方案表）。

## 后果

### 正面

- 交付链少了一整段：应用镜像不再需要从开发机 `save` 后经介质带入，介质传输的对象收缩为「基础镜像 tar」。
- **构建环境与运行环境同机**，消除了 ADR-0006 中「开发机构建产物 vs 生产运行环境」这一层环境偏差。
- 开发机装不了 Docker 这一硬限制**不再阻塞构建**：开发机侧只需能产出 / 传递基础镜像 tar（具体方式未定）。
- 服务器可达内网 yum / PyPI 源 ⇒ 构建期装 wheel 可行，宿主装包也可行——此前「宿主完全装不了包」的判断不再成立（见 D9）。
- 修改交付链**不需要改动服务器上的 Docker 版本**，服务器侧零升级。

### 负面 / 代价

- **构建动作与生产运行共用同一台机器**：构建会占用生产宿主的 CPU、内存、磁盘与 Docker 状态（8 vCPU / 7.6 GiB 是已确认的最终规格；构建峰值占用**未实测**）。构建期间的资源挤占需要运维时留意。
- **生产服务器上必须可达内网 yum / PyPI 源**——这是**构建期**依赖，与「运行时不得依赖任何外部网络」并不冲突，但两条边界必须写清楚，否则容易被误读为「运行时也可以访问内网源」。
- **构建输入必须进入服务器**：Dockerfile、依赖锁定文件、源码都要随构建走一遍；宿主**没有 `git`**，只能靠 `rsync` / 归档传输（D9）。
- **Docker 镜像内容源不可直连消费** ⇒ 基础镜像 tar 的产出与带入仍是一段人工流程，且「由哪台机器产出」未定。
- **在服务器上安装宿主包会走到宿主当前的 yum 源**：该源版本与宿主发行版不匹配的**未授权局部升级风险**已单独登记（见 `docs/open-questions.md` D12）。
- 锁定 codename tag 的代价不变：基础镜像的安全更新不会自动进入，仍需人工定期评估并重建。

## 备选方案与被否决的原因

| 方案 | 否决原因 |
|---|---|
| **继续用开发机 `docker build`（维持 ADR-0006 交付链）** | **已否决**：开发机是未暴露嵌套虚拟化的虚拟机，Docker Desktop 的 WSL2 / Hyper-V 两个后端都依赖硬件虚拟化与 SLAT，**这台机器装不了 Docker** ⇒ 旧交付链的第一步无法执行。 |
| **先把 Docker 装到开发机（给虚拟机开启嵌套虚拟化）** | **已否决**：需要改动宿主 hypervisor 的虚拟机配置，**不可控**，也不在本项目权限范围内。 |
| **放弃容器，改为宿主原生部署**（CentOS 8 上直接装 Python + PostgreSQL） | **未采纳 / 未评估完成**：ADR-0005 已记录过相关冲突（离线 RPM 依赖、宿主系统 Python 3.6.8）。本次**不替所有者否决**该方向，如实登记为**待评估**，由所有者决定。 |
| **要求服务器先把 Docker CLI / Engine 升到 ≥ 28，再继续用 `--platform`** | **沿用 ADR-0006 的否决理由**：服务器是 CentOS 8，Docker 官方已不再支持该宿主，升级路径不明（`docs/open-questions.md` D8 仍在确认）——不能把交付前提建立在一个没有把握的升级上。 |
| **服务器直接从内网 Docker 源 `pull` 基础镜像** | **当前不可行（已知限制）**：该源以路径形式暴露、无独立端点，与 Docker registry 协议要求不符，直连报 content-type 解析错误；问题未解决，本次不要求解决。 |

## 相关

- [0005](0005-offline-docker-deployment-on-centos8.md)：部署形态与「完全离线」表述的出处（其前提表述按本 ADR 决策 (c) 更正，正文保留不动）
- [0006](0006-production-base-image-and-rehearsal.md)：交付链被本 ADR 取代；基础镜像选型与平台约束仍以其为准
- [0010](0010-no-ci-local-gates.md)：不跑 CI ⇒ 构建与预演只能人工执行
- `docs/open-questions.md` D3（开发机装不了 Docker）、D7（image store 已确认）、D8（服务器侧 Docker 可升级路径）、D9（宿主实测规格）、D11（内网源三类覆盖，已确认）、D12（宿主 yum 源与发行版不匹配的风险）
- `deploy/check-image-archive.py`：交付前单平台自检（在新路径上依然适用）
- `AGENTS.md` §8.5：环境前提表述已按本 ADR 更正，**实质约束不变**
