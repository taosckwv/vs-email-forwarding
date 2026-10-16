# 打包：在开发机上产出交付物

> 本文件是**产出侧的操作说明**：在开发机上把应用镜像构建出来、打成交付 tar，并留下一份可随介质一起走的交付清单。
> 读者是开发机上的操作者（人或 AI）。**判据与硬约束的唯一真源是 `docs/research/01-delivery-and-deploy.md`**，
> 脚本职责与用法要点见 `deploy/README.md`；本文件只写「按什么顺序执行哪条命令、期望输出长什么样、失败怎么判读」，
> **不复述结论、不重新定义判据**。
> 拿到 tar 之后怎么装到服务器上，见 `docs/deployment.md`。

---

## 1. 前提

- **在开发机上、仓库根目录执行**。服务器不构建任何东西（判据见 `docs/research/01-delivery-and-deploy.md` 的「结论」段）。
- 本机 Docker 可用，且与生产**同版本、同 image store 模式**（读数与理由见 `docs/research/05-dev-and-build-environment.md`）。
- `Dockerfile` 里两个构建期基础镜像**已按 digest 钉死**（`FROM` 的 `ARG`），本机需已能取到它们。
- **BuildKit 必须开启**：脚本检测到 `DOCKER_BUILDKIT=0` 会**立即失败**——`--platform` 的平台语义由 BuildKit 保证，关掉它可能**静默产出错架构的镜像**（判据见 `docs/research/03-dependencies-and-toolchain.md` 的「构建环境事实」段）。
- 需要 `python3`：两个脚本都用它读 `pyproject.toml` 的版本号（用 `tomllib`，不按行拍字符串）。

## 2. 一次交付：两条脚本 + 一份清单

```bash
# 1) 构建应用镜像（平台只在这一步钉死）
bash deploy/build.sh

# 2) 打包：对每个镜像做「docker save → 归档自检 → 生成 sha256 旁路文件」，最后写交付清单
bash deploy/package.sh              # 首次交付：应用镜像 + postgres:17 两件都打
bash deploy/package.sh --app-only   # 日常发版：只重打应用镜像

# 3) 看清单，确认这一批 tar 是从哪些镜像、哪些基础层做出来的
cat dist/delivery-manifest.txt
```

完整选项与默认值以 `bash deploy/build.sh --help` / `bash deploy/package.sh --help` 的输出为准；要点见 `deploy/README.md`。日常只用得到三条：

| 场景 | 命令 |
|---|---|
| 日常发版 | `bash deploy/build.sh && bash deploy/package.sh --app-only` |
| 首次交付（还要带数据库镜像） | `bash deploy/build.sh && bash deploy/package.sh` |
| 只想看会执行什么 | 加 `--print-command`（build）或 `--print-steps`（package）；验证一次干净构建加 `--no-cache`（build） |

### 2.1 版本号从哪来

默认 tag 是 `vs-email-forwarding:<pyproject.toml 的 [project] version>`，`build.sh` 与 `package.sh` 各自从同一个文件读，**脚本里不写死版本号**。要发别的 tag：先改 `pyproject.toml` 的 `[project] version`，或给两个脚本传**同一个** `--tag`（只给其中一个传，`package.sh` 会因本机找不到那个镜像而失败）。

### 2.2 期望输出

`build.sh` 以构建后的复检收尾：

```text
[信息] 构建完成：vs-email-forwarding:0.1.0
[信息]   image id ：sha256:…
[信息]   平台     ：linux/amd64
[信息]   大小     ：… MB
[信息] 下一步：bash deploy/package.sh            # 首次交付：应用镜像 + postgres:17 两件都打
[信息]         bash deploy/package.sh --app-only # 日常发版：只重打应用镜像
```

`package.sh` 对每件交付物打印三段 + 一行结论（`① 导出` / `② 归档自检` / `③ 生成 sha256 旁路文件` / `✅ <tar> <hash>`），最后写 `dist/delivery-manifest.txt`。

### 2.3 产出物

| 路径 | 是什么 | 进版本库 |
|---|---|---|
| `dist/<镜像名>-<版本>.tar` | 交付镜像归档：应用镜像与 `postgres:17` **各一件** | 否 |
| `dist/<同名>.tar.sha256` | 旁路文件，格式 `<hash>` + 两个空格 + 文件名 + LF | 否 |
| `dist/delivery-manifest.txt` | 交付清单：应用镜像 tag / image id / 平台、两个已钉在 `Dockerfile` 里的构建期 digest、`postgres:17` 的 digest、每件交付物的 sha256 与字节数、带入服务器的四步命令、回滚口径 | 否 |

文件名主干由镜像引用派生：`/` 与 `:` 一律换成 `-`（`vs-email-forwarding:0.1.0` → `vs-email-forwarding-0.1.0.tar`，`postgres:17` → `postgres-17.tar`），**一律 ASCII**。`dist/` 与 `*.tar` 已在 `.gitignore` 里。

## 3. 什么情况下要重打

| 改了什么 | 要做什么 |
|---|---|
| `src/` / `migrations/` / `pyproject.toml` / `uv.lock` / `Dockerfile` | `build.sh` + `package.sh --app-only` |
| `Dockerfile` 的 `FROM` digest（基础层升级） | 同上；新 digest 必须**先复核**再改那一行（读数会漂，见 `docs/research/01-delivery-and-deploy.md` 的「实测证据」段） |
| 换数据库镜像主版本 / 小版本 | `POSTGRES_REF` 指定新镜像后跑一次完整的 `package.sh`（重打 `postgres-*.tar`） |
| 什么都没改，只是再发一次同一版本 | 不必重打，`dist/` 里那份 tar 还是同一批字节 |

`postgres:17` 那份 tar 是**一次性介质**：日常发版用 `--app-only`，不要每版都重传 460MB。

## 4. 失败怎么判读

| 现象 | 含义与处置 |
|---|---|
| `[失败] 检测到 DOCKER_BUILDKIT=0…` | 显式拦截，不是缺陷：`unset DOCKER_BUILDKIT` 后重跑 |
| `docker build` 拉不到基础镜像 | 开发机的镜像获取渠道问题（现状与残余风险见 `docs/research/05-dev-and-build-environment.md`）；**不要在服务器上找补** |
| `镜像 … 的架构是 X/Y，不是 linux/amd64` | 构建命令被改过或 BuildKit 被关；该镜像不可交付 |
| `check_archive.py` 退出码 `1` | tar 不是单平台归档（含 image index / manifest list / attestation）：**该 tar 已被删除，且未生成旁路文件** |
| `check_archive.py` 退出码 `2` | 用法或读取错误（路径写错、文件不可读），不是归档本身不合格 |
| `本机没有镜像 …` | 应用镜像先跑 `build.sh`；运行期镜像先 `docker pull` |
| `sha256sum -c` 失败 | 旁路文件与 tar 不是同一次产出（多见于手工改名或改过文件）：删掉重打 |

顺序是硬约束：**归档自检在生成旁路文件之前**。自检不通过的 tar 会被删掉、且**不生成** `.sha256`——不要手工补一份旁路文件，那正是「带着一份看似已校验的旁路文件被传上服务器」的来源。

## 5. 不要做的事

- ❌ **不要在服务器上构建镜像**：服务器与公网隔离、仓库里没有任何内网源配置，且生产机还要同时跑服务（判据与已否决方向见 `docs/research/01-delivery-and-deploy.md`）。
- ❌ 不要把两个**构建期**基础镜像（Python / uv）也打包带走：它们只在构建机用得到，带进去只是多传约 174MB 且无人消费。
- ❌ 不要把 `dist/` 或任何一个 `*.tar` 提交进仓库。
- ❌ 不要给 `docker save` / `docker load` 传 `--platform`（两侧 Engine 都是 26.1.3 / API 1.45，该参数要 v28 才有）。
- ❌ 不要手工搬运、改名、重生成旁路文件，也不要改动交付物的命名与落点。
- ❌ 不要为了「一次带全」把 `Dockerfile` / `src/` / `migrations/` 也塞进交付目录——它们已烘进应用镜像（见 `docs/deployment.md` 的交付目录布局）。

## 6. 下一步

交付物在手（`dist/` 里两件 tar + 各自 `.sha256` + `delivery-manifest.txt`）→ 按 `docs/deployment.md` 带上服务器：**传输 → 校验完整性 → `docker load` → 载入后逐 tag 复检架构**，再完成首次部署或日常发版。
