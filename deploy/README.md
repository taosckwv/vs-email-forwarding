# mail-relay 离线部署手册

本文件是**操作手册**：照着做就能把服务在离线 CentOS 8 服务器上跑起来。
「为什么这么设计、被否决的备选方案」在 `deploy/offline-deploy.md`，两份文件分工不重叠。

- 本文件（`deploy/README.md`）：**怎么做**——交付物、步骤、校验、回滚、故障排查。
- `deploy/offline-deploy.md`：**为什么**——为什么用离线镜像而不是离线 RPM / wheels，
  为什么基础层用 Debian slim 而不是 CentOS 8，为什么用 almalinux:8 做预演，以及
  目前尚未确定的事项。
- `deploy/centos8-rehearsal/README.md`：**上线前在开发机上做的离线部署预演**。

---

## 0. 一句话流程

```text
开发机（Windows，有网）                     服务器（CentOS 8，完全离线）
─────────────────────────                  ───────────────────────────
deploy\build-image.ps1   构建镜像
deploy\save-image.ps1    打包成 tar + sha256
        │
        └── 介质 / 跳板机（见 §2）──────►  校验 sha256
                                           deploy/load-image.sh
                                           准备 .env
                                           deploy/migrate.sh
                                           deploy/run.sh
                                           验证 /healthz
```

**服务器上不需要 Python，不需要 uv，不需要 pip，不需要访问公网。**
所有依赖（Python 解释器、第三方包、uv 二进制、PostgreSQL）都在镜像里，
镜像通过 `docker load` 落到服务器本地。

---

## 1. 交付物清单

一次完整交付必须包含下面**全部**内容，缺一项都可能现场卡住：

| # | 交付物 | 来源 | 说明 |
|---|---|---|---|
| 1 | `mail-relay-images-<版本>.tar` | 开发机 `deploy/save-image.ps1` | 一个 tar 里同时含 `mail-relay:<版本>`、`mail-relay:latest`、`postgres:17`（预演时还有 `almalinux:8`）。基础镜像必须一起打包，原因见 §3.2 与 `save-image.ps1` 注释。 |
| 2 | `mail-relay-images-<版本>.tar.sha256` | 同上（自动生成） | 传输完整性校验；`load-image.sh` 会自动用它做 `sha256sum -c`。 |
| 3 | `compose.yaml` | 仓库根 | 生产编排。**只写 `image`，不写 `build`**：服务器无网，任何构建路径都会失败。 |
| 4 | `.env` | 由 `.env.example` 复制后填真实值 | **不进版本库**。含数据库口令、SMTP/IMAP 账号、`HTTP_PORT`、`MAIL_RELAY_IMAGE`。 |
| 5 | `deploy/` 目录 | 仓库 | 至少含 `load-image.sh`、`migrate.sh`、`run.sh`（`build-image.ps1`/`save-image.ps1` 是开发机用的，可一带，也可不带）。 |
| 6 | `README` 类文档 | 仓库 | 本文件 + `offline-deploy.md`，便于现场无人可问时自己查。 |

可选项：`deploy/centos8-rehearsal/`（预演环境，通常不必上服务器）。

---

## 2. 传输方式（占位，待所有者补充）

> ⚠️ **待定**：传输通道由所有者后续提供。本节只写清"要传什么、注意什么"，
> 不写死任何介质、主机或路径。

必须满足的要求：

1. **二进制完整性**：介质/跳板机不得对 tar 做任何转换（不能经过会改换行、改编码、
   重新压缩的工具）。tar 是**大文件**（应用镜像 + Postgres 镜像，量级在数百 MB
   到 1 GB 之间），传输中断后要能续传。
2. **sha256 必须随包一起传**，并在服务器侧重新校验（不能只在开发机算一次就算完）。
3. **文件名保持 ASCII**：tar 与 `.sha256` 的文件名不要带中文或空格。CentOS 8 上
   若 locale 是 `POSIX`，shell/tar 处理非 ASCII 路径容易出问题。
4. **`.env` 单独传递且不要经过公共通道**：里面有数据库口令与邮箱凭据。
   建议现场手填，或走加密通道，不要和 tar 一起放在共享目录里。
5. **`platform` 必须一致**：包内镜像是 `linux/amd64`。如果开发机将来换成 arm64
   （Apple Silicon），必须显式 `-Platform linux/amd64` 构建，否则会静默产出错架构镜像。

---

## 3. 服务器侧步骤（按顺序执行）

### 3.0 前置核对：Docker 版本（**别跳过这一步**）

```bash
docker version
docker compose version
```

要求：

| 项 | 要求 | 原因 |
|---|---|---|
| Docker Engine | **≥ 25**，建议 ≥ 28 | 自 moby **v25.0.0** 起 `docker save` 产出 **OCI 归档**；`--platform` 参数要到 Engine **v28** 才有。 |
| Docker CLI | **≥ 28** | `docker image load --platform` 需要；低于此版本 `load-image.sh` 会退化为不带该参数并提醒人工确认架构。 |
| Compose | **v2**（`docker compose` 子命令） | `compose.yaml` 不含 `version:` 字段，且用到 `depends_on.condition: service_healthy`；python 版 `docker-compose` v1 不支持这两点。 |

另外两条必须知道的事实：

- **Docker 官方已不再把 CentOS 8 列为受支持的宿主**（官方安装文档现在只列
  CentOS Stream 9/10）。服务器上那一版 Docker 处于**无官方支持**状态：
  出问题时要自己查社区/源码，不能指望官方修复。这是现状，不是本项目的选择。
- 若服务器是 **classic image store** 且引擎较老，load 多平台归档可能失败
  （classic image store 不支持 image index / manifest list）。遇到「load 成功但
  镜像用不了」时，按 §6「镜像架构不匹配」与本节版本要求排查。

### 3.1 校验完整性

```bash
cd <交付目录>
sha256sum -c mail-relay-images-<版本>.tar.sha256
```

`OK` 才继续。失败说明传输损坏，**重新传**，不要试图加载。

### 3.2 加载镜像

```bash
bash deploy/load-image.sh            # 默认在当前目录找 *.tar
sudo bash deploy/load-image.sh /opt/mail-relay    # 或指定交付目录
```

脚本会：检查 docker 与权限 → 核对版本 → 逐个 `sha256sum -c` → `docker image load --platform linux/amd64` → 打印镜像列表。

**幂等**：可以重复执行。已存在的镜像只会被重新打 tag，不会破坏数据。

为什么必须把 `postgres:17` 一起打包：离线服务器**拉不到任何镜像**（没有 Docker Hub、
没有 ghcr.io、没有内网 registry）。`docker load` 只恢复 tar 里有的东西；tar 里缺
`postgres:17` 时，`docker compose up -d` 会尝试联网拉取并直接失败。

### 3.3 准备 `.env`

```bash
cp .env.example .env
vi .env     # 填真实值：数据库口令、SMTP/IMAP 账号、HTTP_PORT 等
```

本次交付必须确认的键：

- `MAIL_RELAY_IMAGE=mail-relay:<版本>` —— 指向本次交付的版本 tag。
  **不要依赖 `latest`**：回滚时靠的就是这个变量换成旧 tag（见 §5）。
- `HTTP_PORT` —— 对外监听端口，默认 8000。

#### ⚠️ `.env.example` 目前缺 4 个键，交付前必须补（**否则部署起不来**）

当前仓库里的 `.env.example` 只有 `DATABASE_URL`，没有下面这 4 个键。
`compose.yaml` 需要它们，**这是交付前必须由所有者补进 `.env.example` 的改动**
（本目录不能替改 `.env.example`，它由根配置维护）：

```ini
# 官方 postgres 镜像做 initdb 时用的三件套；
# 必须与 DATABASE_URL 里的用户/口令/库名**完全一致**，否则会出现
# 「容器建了 A 库，应用拿 B 凭据去连」这类只报"认证失败"的怪问题。
POSTGRES_USER=mail_relay
POSTGRES_PASSWORD=<与 DATABASE_URL 中的口令相同，不要提交进版本库>
POSTGRES_DB=mail_relay

# 本次交付的镜像 tag（离线服务器上指向已 load 的具体版本）
MAIL_RELAY_IMAGE=mail-relay:0.1.0
```

另外**必须改掉** `DATABASE_URL` 的主机名：`.env.example` 里写的是 `127.0.0.1`，
那是「应用跑在宿主机上连容器里的库」的写法。生产里应用**在容器内**，
`127.0.0.1` 指向应用自己，正确写法是用 compose 服务名：

```ini
DATABASE_URL=postgresql+asyncpg://mail_relay:<口令>@postgres:5432/mail_relay
```

（开发机为了直连本地开发库保留 `127.0.0.1` 无所谓——那种场景下应用确实跑在宿主机上。）

口令、账号一律不要出现在命令历史里（例如别用 `export PGPASSWORD=...` 的方式执行命令）。
仓库里只有 `.env.example`，`.env` 永远不进版本库。

### 3.4 初始化数据目录（仅首次部署）

PostgreSQL 容器内的 postgres 用户是 **uid/gid 999**，宿主机目录必须先给它：

```bash
mkdir -p ./data/postgres
chown -R 999:999 ./data/postgres
```

如果服务器 SELinux 处于 `enforcing`，还需要给该目录打标签，否则 Postgres 会因
「permission denied」起不来。**具体用挂载选项 `:z`/`:Z` 还是 `chcon`，
本项目尚未取得官方原文确认，标注为待确认**——交付前按 §6 现场实测一次并回填结论。

```bash
getenforce                      # Enforcing 才需要处理
ls -Zd ./data/postgres          # 看当前标签
```

### 3.5 执行数据库迁移

```bash
bash deploy/migrate.sh
```

实际执行的是 `docker compose run --rm app uv run alembic upgrade head`。

> ⚠️ **Alembic 尚未初始化**：`migrations/` 与 `alembic.ini` 要等数据模型规格落地后
> 才会生成（见 `docs/open-questions.md` A1–A5）。在那之前这一步**一定失败，属预期**。
> 判别方法：报错是「找不到 alembic.ini / migrations / alembic 命令」= 规格未落地；
> 报错是「连不上数据库」= 真正的部署问题。
> 镜像里带着 uv 正是为了这一步（离线服务器不能临时装工具）。

### 3.6 启动服务并健康检查

```bash
bash deploy/run.sh
```

脚本会 `docker compose up -d`，然后对 `http://127.0.0.1:${HTTP_PORT}/healthz` 轮询
（默认 30 次 × 2 秒，可用 `HEALTH_RETRIES` / `HEALTH_INTERVAL` 覆盖），
成功后打印容器状态与日志命令，失败则给出排查清单。

`up -d` 返回 0 **不等于**服务可用，所以一定要等健康检查通过。

### 3.7 验证

```bash
curl -i http://127.0.0.1:8000/healthz        # 端口按 .env 的 HTTP_PORT
docker compose -f compose.yaml ps           # 两个容器都应 healthy / running
docker compose -f compose.yaml logs --tail=100 app
```

对外放行（若需要从别的机器访问）：

```bash
firewall-cmd --permanent --add-port=8000/tcp && firewall-cmd --reload
```

数据库端口**不要**对外放行：应用与库在同一个 compose 网络里用服务名互访，
生产 `compose.yaml` 里也没有映射 5432。

---

## 4. 日常运维

```bash
docker compose -f compose.yaml ps                     # 状态
docker compose -f compose.yaml logs -f app            # 应用日志
docker compose -f compose.yaml restart app            # 重启应用
docker compose -f compose.yaml down                   # 停止（保留 ./data/postgres 数据）
```

**备份**：数据就在宿主机目录 `./data/postgres` 里，停机后直接打包该目录即可
（热备请用 `pg_dump`，不要直接拷正在写入的数据目录）。

日志纪律（见 `AGENTS.md` §8）：日志里不得出现邮件正文、收件人地址、凭据；
排查时用邮件 ID 引用，不要贴正文。

---

## 5. 回滚

镜像 tag 是版本化的，回滚**不需要重新构建**：

1. 服务器上保留至少上一个版本的镜像（`docker image ls` 里能看到，`load` 新版本时不会删旧的）。
2. 修改 `.env`：

   ```ini
   MAIL_RELAY_IMAGE=mail-relay:<上一个版本>
   ```

3. 重新起：

   ```bash
   bash deploy/run.sh
   ```

4. 数据库结构回滚：如果新版本包含破坏性迁移，需要**单独**的降级步骤
   （`alembic downgrade`），不能只回滚镜像。这条在 Alembic 落地后要补进本手册。

如果旧版本镜像被清理掉了，只能从介质里重新 `docker load` 上一次的 tar
（这也是为什么建议**至少保留最近两次**的交付 tar）。

---

## 6. 常见故障与排查

| 症状 | 可能原因 | 处理 |
|---|---|---|
| `docker compose up -d` 报「pull access denied / no such image」 | tar 里没带基础镜像，或 `MAIL_RELAY_IMAGE` 写错 | 确认 tar 内含 `postgres:17`（`tar -tf <tar> \| grep -i manifest`）；确认 `MAIL_RELAY_IMAGE` 指向已 load 的 tag |
| `exec format error` / 容器起一秒就退 | 镜像架构不是 `linux/amd64`（开发机是 arm64 时易发） | `docker image inspect --format '{{.Os}}/{{.Architecture}}' <镜像>`；重新用 `-Platform linux/amd64` 构建并重传 |
| `docker image load` 报 manifest / index 相关错误 | 服务器 Engine < 25 或用的 classic image store（不支持 image index） | 按 §3.0 核对并升级 Engine；必要时改在服务器上重新构建（不推荐，服务器无网） |
| 端口占用：`bind: address already in use` | 8000 被别的进程占 | `ss -lntp \| grep 8000`；停掉占用进程，或改 `.env` 的 `HTTP_PORT` |
| 从外部连不上 `/healthz`，本机 `curl` 正常 | firewalld 未放行 | `firewall-cmd --permanent --add-port=<端口>/tcp && firewall-cmd --reload`；`firewall-cmd --list-ports` 确认 |
| Postgres 起不来，日志 `permission denied` / `initdb: could not change permissions` | `./data/postgres` 属主不是 999 | `chown -R 999:999 ./data/postgres`（见 §3.4） |
| Postgres 日志出现 SELinux `avc: denied` | 目录标签不对（enforcing） | 用 `:z`/`:Z` 挂载选项或 `chcon` 重打标签；**具体做法为待确认项**，现场实测后回填本节 |
| 应用起不来、日志报数据库连接失败 | `.env` 里连接参数错；或 postgres 还没 healthy | `docker compose ps` 看 postgres 健康状态；核对 `.env` 的 host 必须是服务名 `postgres`（不是 127.0.0.1） |
| `/healthz` 一直不通，容器却在 running | 应用内部启动失败（lifespan 抛错、四条流初始化失败） | `docker compose logs --tail=200 app` |
| `bash deploy/*.sh` 报 `$'\r': command not found` | 脚本在 Windows 下被转成了 CRLF | `sed -i 's/\r$//' deploy/*.sh`，或交付前确保 git 保持 LF（本仓库的 `* .sh` 应以 LF 提交） |

排查通用三步：`docker compose ps` → `docker compose logs postgres` → `docker compose logs app`。

---

## 7. 安全与合规红线（部署时同样成立）

- `.env` 不进版本库、不进聊天记录、不进 ticket。
- 日志/告警里不出现邮件正文、附件内容、收件人地址、原始 MIME、账号口令。
- 服务器不需要也不应该开放公网出口；若必须放行端口，只放应用端口，不放数据库端口。
- 本项目**不跑 CI**：能验证的都在开发机的 `scripts/check.ps1` / `check.sh`，
  以及上线前的 `deploy/centos8-rehearsal/` 预演里验证。
