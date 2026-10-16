# 部署：两个 tar 到手之后怎么装、怎么发版、怎么回滚

> 本文件是**消费侧的操作说明**：服务器上的首次部署、日常发版、回滚与备份各敲哪些命令、怎么验收与判读。
> **判据与硬约束的唯一真源是 `docs/research/01-delivery-and-deploy.md`**，脚本职责与失败分类见 `deploy/README.md`；
> 本文件只写操作顺序与验收口径，**不复述结论**。交付物是怎么产出的，见 `docs/packaging.md`。

---

## 1. 目标环境（先对齐这一句）

CentOS 8 生产宿主：**与公网隔离**、可达内网镜像源，**运行期不得依赖任何外部网络**；宿主有 Docker（Client / Server 同为 26.1.3、**API 1.45**）与 **compose v2**，**没有 `git`**；SELinux `Enforcing`、firewalld active；应用与 `postgres:17` **同机、同一个 compose**，数据落宿主机目录 `./data/postgres`。全部读数与结论见 `docs/research/01-delivery-and-deploy.md`。

## 2. 要带什么、不用带什么

| 介质 | 说明 |
|---|---|
| `vs-email-forwarding-<版本>.tar` + `.tar.sha256` | 应用镜像，**每次发版都要传** |
| `postgres-17.tar` + `.tar.sha256` | 运行期数据库镜像，**一次性介质** |
| `compose.yaml` | 生产编排（只有 `image`、没有 `build`） |
| `.env` | 真实配置（**单独传，绝不进版本库**） |
| `deploy/` 整个目录 | `migrate.sh` / `run.sh` / `check_archive.py` / `README.md` |
| `delivery-manifest.txt` | 交付清单：建议一并带上，人工比对 hash 与 digest 用；不是运行必需 |

**不用带**：`Dockerfile`、`src/`、`migrations/`、`pyproject.toml`、`uv.lock`——它们已烘进应用镜像（`migrations/` 在镜像内，迁移用 `docker compose run --rm app` 跑）。

交付目录长这样，`docker compose` **就在这个目录里执行**（compose 用相对路径 `./data/postgres`，`deploy/run.sh` 默认取脚本上一级的 `compose.yaml`）：

```text
<交付目录>/
├── compose.yaml
├── .env
├── deploy/                         # 含 migrate.sh / run.sh / check_archive.py / README.md
├── vs-email-forwarding-<版本>.tar + .tar.sha256
├── postgres-17.tar               + .tar.sha256
├── delivery-manifest.txt           # 建议带上
└── data/postgres/                  # 第 3 步创建，postgres 数据落在这里
```

## 3. 首次部署（按顺序做）

### 第 0 步 宿主前置检查

```bash
docker version --format '{{.Server.Version}} / API {{.Server.APIVersion}}'   # 期望 26.1.3 / 1.45
docker compose version                                                        # 必须是 v2 子命令
getenforce                                                                    # Enforcing：第 3 步要重打标签
command -v sha256sum python3                                                  # 校验与探测都要用
```

`curl` 缺失不影响启动：`deploy/run.sh` 会降级为在容器内用 Python 标准库探测 `/healthz`。

### 第 1 步 传入介质（在**产出机**上执行）

```bash
rsync -av --partial dist/*.tar dist/*.sha256 <用户>@<服务器>:<交付目录>/
rsync -av --partial compose.yaml deploy/ <用户>@<服务器>:<交付目录>/
# .env 单独传：它含凭据，不走共享渠道
```

传输渠道的要求：**可续传、不得改动二进制内容、文件名一律 ASCII、`.env` 单独传**（判据见 `docs/research/01-delivery-and-deploy.md` 的「脚本必须保留的方法性结论」段）。

### 第 2 步 准备 `.env`

复制 `.env.example` 为 `.env` 后逐项填；**逐项用途与默认值见 `docs/configuration.md`**。留空或留假值就会出事的有这几项：

- `DATABASE_URL`：指向真实库的连接串；
- `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB`：compose 给 postgres 做 initdb 用，**三处必须与 `DATABASE_URL` 一致**；`POSTGRES_PASSWORD` 没有默认值（compose 写的是 `${POSTGRES_PASSWORD:?}`，不填 `up` 直接失败），口令**不要含** `$` `@` `:` `/`（compose 插值与 URL 解析都会出错）；
- `MAIL_FROM_ADDRESS` / `REPLY_TO_ADDRESS` / `IMAP_ADDRESS`：回复回路的主角；
- `SMTP_HOST`（+ 账号口令）与 `IMAP_HOST`（+ 账号口令）：留空时对应方向的功能不可用；
- `VS_EMAIL_FORWARDING_IMAGE`：**要跑的那个 tag**（回滚就改这里）。

**启动校验三判据**（`config.py::validate_startup()`，在 lifespan 的显式启动步骤执行；**任一不满足即拒绝启动、不监听端口**，三条一次性全部检查后统一报出）：

1. `IMAP_ADDRESS` 与 `REPLY_TO_ADDRESS` **相等且非空**（不一致时回复回路是静默断掉的）；
2. `MAIL_FROM_ADDRESS` 能解析出邮件域（出站 `Message-ID` 的域取自它）；
3. `REPLY_TO_ADDRESS` 本身是合法地址（含 `@`、无空白）。

校验信息里只出现**配置项名**，不出现地址本身。

### 第 3 步 建数据目录并处理 SELinux 标签

```bash
mkdir -p ./data/postgres && chown -R 999:999 ./data/postgres
# SELinux 为 Enforcing，挂载点必须重打标签：用挂载选项 :z / :Z，或等价的 chcon
```

属主不对 postgres 起不来；标签不对会被 SELinux 拒绝访问挂载点——**这不是待确认项**（判据见 `docs/research/01-delivery-and-deploy.md` 的「服务器侧硬约束」段）。

### 第 4 步 校验完整性

```bash
cd <交付目录>
sha256sum -c --status vs-email-forwarding-<版本>.tar.sha256 && echo 应用镜像完整
sha256sum -c --status postgres-17.tar.sha256 && echo 数据库镜像完整
```

- **必须先 `cd` 到 tar 所在目录**：旁路文件里写的是**文件名**，否则会按产出机的路径去找文件。
- 旁路文件缺失时，打印实算 hash 供人工比对——这是**降级路径，不是直接失败**。

### 第 5 步 载入镜像

```bash
docker load -i vs-email-forwarding-<版本>.tar
docker load -i postgres-17.tar
```

- **一律不传 `--platform`**（API 1.45 不支持该参数，传了直接失败）。
- 要读 `docker load` 的输出就用 `sed -n` 而**不要用 `head`**：`head` 会让 docker 收到 SIGPIPE，在 `set -o pipefail` 下变成假失败。
- 同 tag 再次 `load` 会覆盖本地同名 tag 的镜像——回滚要用的旧版本请**换新 tag 发版**，并保留旧镜像与旧 tar。

### 第 6 步 载入后逐 tag 复检架构

```bash
docker load -i <tar> 2>&1 | sed -n 's/^Loaded image: //p'      # 枚举这次载入了哪些 tag
docker image inspect --format '{{.Os}}/{{.Architecture}}' <每个 tag>
```

**任一 tag 不是 `linux/amd64` 即判定失败，不要继续往下走**（否则会在运行期变成 `exec format error`）。`deploy/run.sh` 启动前会对应用镜像与 compose 依赖镜像逐个做同一强度的复检，那是这一步的自动化等价物，手动复检仍建议保留在载入当场，两者不互斥。

### 第 7 步 迁移数据库

```bash
bash deploy/migrate.sh
```

- 命令形状固定在脚本内一处：`docker compose run --rm app uv run alembic -c migrations/alembic.ini upgrade head`——用 **`run --rm`** 而不是对跑着的容器 `exec`，且**不加 `--no-deps`**，让 compose 先把数据库等到 healthy。
- `upgrade head` **幂等，重跑安全**。
- `migrations/` 里还没有版本时，首次迁移**必然失败、属预期**（脚本会把这条判别法打印出来）。
- 四类失败（① 结构性、② 连接与库级、③ 迁移脚本自身、④ 运行环境）见 `deploy/README.md`。第 ④ 类的报错是 `Failed to spawn` / `Failed to download` + dns error，含义是镜像构建期的 `.venv` 不完整，**修法是回构建机重建镜像**，不是在这台机器上补装依赖。

### 第 8 步 起服务并探活

```bash
bash deploy/run.sh
```

- 顺序固定：**先迁移、再起服务**（默认先调 `migrate.sh`，`--skip-migrate` 可跳过）→ `docker compose up -d` → 轮询 `/healthz`（默认 30 次 × 2s）。**`up -d` 返回 0 不等于服务可用**。
- 失败时脚本打印排查清单（容器状态 + 日志尾部），并提醒日志脱敏。

### 第 9 步 放行端口（供内网调用方访问）

```bash
firewall-cmd --permanent --add-port=<HTTP_PORT>/tcp && firewall-cmd --reload
```

**`firewall-cmd --reload` 不得在容器运行期间执行**——reload 会冲掉 Docker 插入的 iptables 链，可能导致容器网络中断。数据库端口**不对宿主机开放**：应用与库在同一张 compose 网络里用服务名 `postgres` 互访。

### 第 10 步 验收

```bash
curl -sS http://127.0.0.1:<HTTP_PORT>/healthz      # 期望 200，体为 {"status":"ok","app_env":"prod","version":"…"}
docker compose -f compose.yaml ps                  # app 与 postgres 都 Up / healthy
docker compose -f compose.yaml logs --tail=50 app  # 五条流各自有推进日志；确认无邮件正文、收件人地址、凭据
```

- `/healthz` 的语义只有「**进程可用** + 一次轻量数据库探测」，**不表达五条后台流是否在跑**——流是否推进看日志与独立探针（判据见 `docs/research/02-runtime-and-scheduling.md`）。
- **隔离前提的验证（`AC-20`）只能在服务器上做**：容器起来后看**容器内**的 `/proc/net/tcp`，观察窗口内应只有到 PostgreSQL 的连接与自身的监听端口（含 Docker 内嵌 DNS `127.0.0.11`），不得出现公网或内网源连接。宿主级 `ss` 会被运维侧会话污染，**不能**作为判据。
- 接口怎么调、怎么判读，见 `docs/api.md`。

## 4. 日常发版（tar 已经有了）

产出机：改 `pyproject.toml` 的版本号 → `bash deploy/build.sh` → `bash deploy/package.sh --app-only`（见 `docs/packaging.md`）。服务器侧：

```bash
cd <交付目录>
sha256sum -c --status vs-email-forwarding-<新版本>.tar.sha256
docker load -i vs-email-forwarding-<新版本>.tar
docker image inspect --format '{{.Os}}/{{.Architecture}}' vs-email-forwarding:<新版本>
# 把 .env 的 VS_EMAIL_FORWARDING_IMAGE 改成新 tag
bash deploy/run.sh          # 内部先跑迁移，再 up -d + 探活
```

`postgres-17.tar` 不重传（除非要换数据库镜像）。服务器上建议至少保留**最近两个版本**的镜像与交付 tar。

## 5. 回滚（刻意不脚本化）

1. 把 `.env` 的 `VS_EMAIL_FORWARDING_IMAGE` 改回**上一个版本 tag**，再 `docker compose -f compose.yaml up -d`——**不需要重新构建**（上一个版本的镜像与 tar 本机都还在）。
2. **只有当本次发版含破坏性迁移时**，才另跑一次降级：

```bash
docker compose -f compose.yaml run --rm app uv run alembic -c migrations/alembic.ini downgrade <目标版本>
```

第二步是否执行取决于**本次迁移的内容**，脚本判错会把库降级到与镜像不匹配的版本——因此回滚步骤由交付时逐次说明，**不落 `deploy/rollback.sh`**，也不要自己补一个。

## 6. 备份

- **停机**：打包数据目录 `./data/postgres`。
- **热备**：用 `pg_dump`，**不要直接拷正在写入的数据目录**。

保留期与日志采集策略尚未决定，见 `docs/open-questions.md`。

## 7. 排障速查

| 现象 | 先看哪儿 |
|---|---|
| `/healthz` 返回 503 | 数据库不可用：`docker compose ps` / postgres 健康检查 / `DATABASE_URL` 与 `POSTGRES_*` 三处是否一致 |
| 应用容器反复重启 | 启动校验三判据（第 2 步）——不满足即拒绝启动、不监听端口，日志里只出现配置项名 |
| 迁移报 `Failed to spawn` / `Failed to download` + dns error | 失败分类④：镜像 `.venv` 不完整，回构建机重建镜像 |
| `exec format error` | 镜像架构不对：回到第 6 步逐 tag 复检 |
| 容器起来了但外部连不上 | 宿主机端口放行（第 9 步）与 `.env` 的 `HTTP_PORT`（`run.sh` 解析时会容忍 CR / 引号 / 行尾注释） |
| 收不到回复 / 发不出去 | `SMTP_HOST` / `IMAP_HOST` 是否留空（留空时对应流不可用，但**不**在启动校验三判据内），以及 `IMAP_ADDRESS` 与 `REPLY_TO_ADDRESS` 是否一致 |
| 怀疑有外部网络访问 | 容器内 `/proc/net/tcp`（第 10 步），不要看宿主 `ss` |

## 8. 边界

- 本文件是**操作说明**：交付链的形态与判据以 `docs/research/01-delivery-and-deploy.md` 为准，**不要擅自新建部署脚本**——带入与 `docker load` 刻意保持手动，脚本里不写服务器地址、账号或凭据。
- 不得在本文件里写真实主机名、端口、账号——一律占位符。
- 服务器侧 Docker 是否有可升级路径、宿主 yum 源版本与发行版错配，都是未定项，见 `docs/open-questions.md`。
