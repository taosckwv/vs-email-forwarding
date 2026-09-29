# CentOS 8 离线部署预演

这是**上线前的预演环境**：在开发机上用 `almalinux:8` 容器模拟离线服务器
（RHEL 8 / CentOS 8 用户态），把「load → 迁移 → 启动 → 健康检查」这套交付流程
先走一遍，提前暴露环境差异与脚本问题。

- 操作手册（正式步骤）：`deploy/README.md`
- 设计理由（为什么这么选）：`deploy/offline-deploy.md`
- 本文件：预演**要验证什么、不验证什么、和真实 CentOS 8 差在哪**。

---

## 1. 快速开始

```bash
# 1) 起预演容器（空壳，常驻）
docker compose -f deploy/centos8-rehearsal/compose.yaml up -d

# 2) 进容器（交付目录挂在 /delivery，只读）
docker compose -f deploy/centos8-rehearsal/compose.yaml exec rehearsal bash

# 3) 在容器里按下面 §3 的清单逐项检查，然后退出

# 4) 关掉
docker compose -f deploy/centos8-rehearsal/compose.yaml down
```

容器里**不装 Python、也不装 uv**——这不是省事，而是刻意的约束：
目标服务器上也没有这两样，预演环境必须同样"什么都没有"才能暴露真实问题
（如果预演环境装了 Python，很多"其实依赖了宿主机工具"的错误就永远发现不了）。

---

## 2. 预演覆盖什么、不覆盖什么（最重要的一节）

### 2.1 覆盖

| 覆盖项 | 预演里怎么验 | 为什么值得验 |
|---|---|---|
| **RHEL 8 用户态 ABI（glibc 2.28）** | `ldd --version` | 确认交付脚本用到的工具链与目标同代；Python 侧由镜像自带 glibc，不受影响 |
| **交付脚本的 shell 行为** | 对 `deploy/*.sh` 跑 `bash -n`，并实际执行不含 docker 的部分 | `set -euo pipefail`、`[[ ]]`、`seq`、`command -v`、`tr -cd` 等在各版本 bash/coreutils 下行为有差异 |
| **tar 的可用性与文件名** | `tar -tf` 列出交付 tar；检查有无非 ASCII 路径 | 中文/空格路径 + `POSIX` locale 是最经典的现场翻车点 |
| **完整性校验链路** | `sha256sum -c <tar>.sha256` | 确认旁路校验文件是 LF、格式与 `sha256sum -c` 兼容 |
| **RPM / dnf 行为**（若需要装系统包） | `dnf --version`、`rpm -q` | 现场若被迫装包，至少要能判断 dnf 源是否可用 |
| **各类 profile 差异** | `bash -lc` 与 `bash -c` 两种方式各跑一次脚本 | CentOS 8 上 `/etc/profile.d` 可能与开发机不同 |

### 2.2 不覆盖（**必须由真实 CentOS 8 兜底**）

| 不覆盖项 | 原因 |
|---|---|
| **单元测试与集成测试** | 预演环境里根本没有项目代码的运行环境（无 Python/uv）。测试在**开发机**跑：`scripts/check.ps1` / `scripts/check.sh`（ruff + mypy + pytest + PostgreSQL/Mailpit 集成测试）。 |
| **内核行为** | 预演容器跑在开发机 Docker Desktop 上（**WSL2 内核 6.6**），目标宿主 CentOS 8 是 **4.18**。cgroup v1/v2、iptables vs nftables、`sysctl` 默认值、overlayfs 行为都可能不同。**内核级问题预演一定发现不了。** |
| **真实 SELinux** | 预演容器里的 SELinux 与宿主不同（Docker Desktop 的 Linux VM 基本不启用 enforcing 策略）。挂载标签这件事只能在真机上验。 |
| **firewalld / 网络策略** | 预演容器没有 firewalld 服务，也无法模拟真实网段的放行规则。 |
| **磁盘容量与真实数据目录的 I/O 特性** | Postgres 数据目录在 NFS/本地盘/不同文件系统上的行为差异，预演无法覆盖。 |
| **Docker / Compose 版本差异** | 预演用的是开发机上的新版 Docker Desktop；服务器上很可能是老版本（CentOS 8 常见 20.10/24/26，且 Docker 官方已不再支持 CentOS 8 作为宿主）。**这一项必须在服务器上用 `docker version` 核对**（见 `deploy/README.md` §3.0）。 |
| **镜像自身的 glibc 兼容性** | 应用/数据库镜像自带 glibc（Debian），不依赖宿主 glibc；所以"预演通过"并不等于"证明 glibc 兼容"。 |

> 一句话：**预演验证的是「流程与用户态」，不是「内核与 ABI 的最终兼容性」。**
> 预演通过 ≠ 可以不上真机验收。

### 2.3 与 ADR-0006 里列出的验证目标的对应关系

`docs/adr/0006-production-base-image-and-rehearsal.md` 把预演的验证目标写成 5 条。
本预演环境**逐一对应**如下（注意执行位置：容器内只做用户态与交付物检查，
需要 docker 的步骤在**宿主机**上执行）：

| ADR-0006 的验证目标 | 在哪里执行 | 怎么验 |
|---|---|---|
| `docker load` 镜像 tar 能否成功 | **宿主机**（预演容器里没有 docker CLI，也未联网安装） | `bash deploy/load-image.sh <交付目录>`；容器内可先做 `sha256sum -c` 与 `tar -tf` 预检（§3 步骤 2–3） |
| `docker run` 能否正常启动 | **宿主机** | `bash deploy/run.sh`（内含 `/healthz` 轮询） |
| Alembic 迁移能否跑通 | **宿主机** | `bash deploy/migrate.sh`（当前必然失败，属预期，见脚本注释） |
| 应用能否连上同机 PostgreSQL 容器 | **宿主机** | `docker compose ps` 看 postgres healthy；`run.sh` 的健康检查通过即说明应用连库成功 |
| 服务启停与日志输出是否符合预期 | **宿主机** | `docker compose logs app`、`docker compose restart app`、`docker compose down` |

为什么 docker 命令不在预演容器里执行：该容器是**空壳的 RHEL 8 用户态**，
里面没有 docker CLI，也没有网络去安装它。要么把它挂 `/var/run/docker.sock`
（等于把宿主 root 权限交给一个空壳容器，收益很低、风险很高，默认关闭），
要么按上表在宿主机上执行——**推荐后者**。

换句话说明本环境的定位：**预演 = almalinux:8 用户态检查（本容器）+ 宿主机上的
docker 流程执行**，两者合起来才算把 ADR-0006 的 5 条验证目标走完。

---

## 3. 预演检查清单（在容器里执行）

```bash
# --- 0. 环境自证 -------------------------------------------------------------
cat /etc/redhat-release          # 应为 AlmaLinux release 8.x
ldd --version | head -n 1        # glibc 应为 2.28（与 CentOS 8 同代）
bash --version | head -n 1
rpm --version
sha256sum --version | head -n 1
tar --version | head -n 1
locale                            # 注意是否 POSIX/C —— 非 ASCII 路径的风险点

# --- 1. 交付物存在且是 ASCII 文件名 -----------------------------------------
ls -l /delivery
# 交付目录是只读挂载；tar 在 dist/ 下（若已生成）
ls -l /delivery/dist 2>/dev/null || echo "（还没跑 save-image.ps1，属正常）"

# --- 2. 脚本语法检查（不需要 docker 的部分） --------------------------------
for f in /delivery/deploy/*.sh; do echo "== $f"; bash -n "$f"; done
# 预期：全部无输出（语法通过）

# --- 3. 校验 + 解包 tar（不依赖 docker，可完整验证传输完整性） --------------
cd /tmp
cp /delivery/dist/*.tar .
cp /delivery/dist/*.tar.sha256 .
sha256sum -c ./*.tar.sha256                 # 预期：OK
tar -tf ./*.tar | head -n 30                # 预期：能看到 manifest.json / *.tar 层
tar -tf ./*.tar | grep -P '[^\x00-\x7F]' && echo "发现非 ASCII 路径！" || echo "路径全 ASCII（通过）"
```

真正需要 docker 的命令（`docker load` / `migrate.sh` / `run.sh`）**在宿主机上执行**：

```bash
# 在开发机的仓库根（不是在这个容器里）执行
bash deploy/migrate.sh          # Alembic 未初始化时会失败，属预期
bash deploy/run.sh              # 起服务 + 健康检查
```

若确实想在容器内调用宿主 Docker，需要镜像内有 docker CLI —— 本镜像没有，
也没有联网安装。**推荐做法是不要在预演容器里跑 docker 命令**：
`docker.sock` 交给空壳容器等于给出 root 级权限，收益却很低。

---

## 4. 与真实 CentOS 8 的差异（逐条列清）

| 维度 | 预演（almalinux:8 容器，跑在开发机） | 真实 CentOS 8 宿主 |
|---|---|---|
| 用户态 | AlmaLinux 8.10，glibc 2.28 | CentOS 8（最终 8.5 系列），glibc 2.28 |
| 内核 | **宿主提供**：Docker Desktop 的 WSL2 内核（约 6.6） | 4.18（CentOS 8 自带） |
| cgroup | 通常是 cgroup v2 | 多为 cgroup v1 |
| SELinux | 容器内基本无 enforcing 策略 | 常见 `enforcing`（挂载标签问题只在这里出现） |
| firewalld | 无 | 有，且默认拦端口 |
| Docker / Compose | 开发机上的新版（Engine ≥ 28 常见） | 可能是 20.10 / 24 / 26，且**官方已不支持 CentOS 8 宿主** |
| 网络 | Docker Desktop 的 NAT | 真实网段、可能有代理与访问控制 |
| 磁盘 | 开发机 SSD | 现场可能的本地盘/RAID/NFS |

**结论**：预演能挡住"脚本写错、tar 传坏、流程漏步骤"这类问题（成本最低、收益最高），
但**挡不住**内核、SELinux、firewalld、老版本 Docker 这类问题——这些必须在真机上按
`deploy/README.md` 逐项核对。

---

## 5. 关于镜像选择（为什么是 almalinux:8）

- **首选 `almalinux:8`**：与 RHEL 8 / CentOS 8 同 ABI（glibc 2.28），
  仍在**活跃维护**（8.10，近月仍在构建），dnf 源开箱可用。
- 次选 `rockylinux/rockylinux:8`：镜像冻结在 8.10（2024-06-05），
  但发行版支持到 2029-05-31，repo 在线；用之前确认不需要新包。
- **不要用 `library/rockylinux:8`**：它停在 `8.9.20231119`（2024-05-30），
  官方 README 已明确让改用 `rockylinux/rockylinux`。
- **不要用 `centos:8`**，并且要纠正一个常见说法：`centos:8` **并没有从 Docker Hub
  下架**，它**仍然可以 pull**。但它实际等于 **8.4.2105**（`centos:8.5.2111`
  在 Docker Hub 上不存在，404），而且 `library/centos` 的全部 tag 都已标记
  EOL / DEPRECATED，`mirror.stream.centos.org` 已无 8-stream，只剩 vault，
  且 vault **明确不提供安全更新**。所以它不适合做预演基线。

预演镜像不需要进离线交付包；只有当运维希望**在服务器上**也留一份预演环境时才需要
（`deploy/save-image.ps1 -IncludeRehearsal` 会把 `almalinux:8` 一起打进 tar）。
