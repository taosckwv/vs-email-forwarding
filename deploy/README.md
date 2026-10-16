# deploy/ — 部署脚本与交付链的落点

本目录放**交付链与服务器侧部署脚本**：产出侧的镜像构建与打包、交付前归档自检，以及消费侧的数据库迁移入口、服务启动与健康检查；不放应用代码（那在 `src/`）。判据与硬约束的唯一真源是 `docs/research/01-delivery-and-deploy.md`，本文件只写职责、用法要点与判据出处，不复述其结论。

## 产出方一侧（在开发机上跑）

### `build.sh` — 构建应用镜像

- **职责**：在**开发机**上执行 `docker build --platform linux/amd64`（平台只在这一步钉死），构建后复检镜像架构必须是 `linux/amd64`；默认 tag 取 `pyproject.toml` 的 `[project] version`。
- **用法要点**：`bash deploy/build.sh`；`--tag TAG` 指定 tag；`--no-cache` 验证一次干净构建；`--print-command` 只打印命令不执行。
- **显式拦截**：检测到 `DOCKER_BUILDKIT=0` 就立即失败——`--platform` 的平台语义由 BuildKit 保证，关掉它可能静默产出错架构镜像（判据出处：`docs/research/03-dependencies-and-toolchain.md` 的「构建环境事实」段）。
- **判据出处**：`docs/research/01-delivery-and-deploy.md` 的「结论」段（构建在开发机、平台只在构建期钉死、构建期基础镜像按 digest 钉在 `Dockerfile` 的 `FROM` 里）。

### `package.sh` — 导出、自检、生成 sha256 与交付清单

- **职责**：对每个要带入服务器的镜像依次做「`docker save` → 归档自检 → 生成 sha256 旁路文件」，最后写一份交付清单；产出物落 `dist/`（该目录与 `*.tar` 已在 `.gitignore` 里）。
- **用法要点**：`bash deploy/package.sh`（应用镜像 + `postgres:17` 两件都打）；`--app-only` 日常发版只重打应用镜像；`--tag` / `--out-dir` / `--print-steps`；环境变量 `POSTGRES_REF` 可覆盖运行期镜像，`OUT_DIR` 同 `--out-dir`。
- **顺序是硬约束**：归档自检必须在生成 `.sha256` 之前——自检不通过的 tar 会被删掉、且不生成旁路文件，避免它带着一份「看似已校验」的旁路文件被传上服务器。
- **判据出处**：`docs/research/01-delivery-and-deploy.md` 的「脚本必须保留的方法性结论」段（单平台归档、双布局、期望镜像个数显式、旁路文件时序与格式、交付物命名）。

### `check_archive.py` — 交付前归档自检

- **职责**：只读检查 `docker save` 产出的交付 tar 是否为**单平台 `linux/amd64`** 归档；**不依赖 Docker、只用 Python 标准库**，在 tar 进入服务器之前执行（`package.sh` 会自动调用它）。
- **用法要点**：`python3 deploy/check_archive.py <tar>`；`--expected-images N` 显式指定期望镜像个数（默认 **1**：一个 tar 一件镜像——应用镜像与 `postgres:17` 各一件、两次调用）；支持 OCI（`index.json`）与 legacy docker-archive（`manifest.json`）双布局；退出码 `0` 通过 / `1` 归档不合格 / `2` 用法或读取错误；**必须在生成 `.sha256` 旁路文件之前跑**——自检不通过就不要生成旁路文件、更不要传上去。
- **判据出处**：`docs/research/01-delivery-and-deploy.md` 的「脚本必须保留的方法性结论」段（单平台归档、双布局、期望个数显式、旁路文件时序、退出码语义）。

## 消费方一侧（在服务器上跑）

### `migrate.sh` — 数据库迁移入口（部署时执行 Alembic 的唯一入口）

- **职责**：在交付目录里用应用镜像跑一次性容器，执行 `docker compose run --rm app uv run alembic -c migrations/alembic.ini upgrade head`。命令形状固定在脚本内一处，不做第二入口。
- **用法要点**：`bash deploy/migrate.sh`；`--print-command` 只打印命令不执行；`--compose-file F`（或环境变量 `COMPOSE_FILE`）指定 compose 文件，默认取脚本上一级的 `compose.yaml`；`--skip-probe` / `SKIP_PROBE=1` 跳过「镜像内是否有 `migrations/`」预检。
- **失败分类（四类）**：① 结构性故障（镜像/工作目录里没有 `migrations/`，含 `alembic.ini` / `env.py` 缺件，有只读预检）；② 连接/库级故障；③ 迁移脚本自身错误（`upgrade head` 幂等，重跑安全）；④ 运行环境故障——镜像构建期 `uv sync` 未生效（`.venv` 不完整）。①②③ 与「※ 无版本判别式」在 `--help` 里；④ 只在失败输出里打印（脚本头注也指向它）。
- **判据出处**：`docs/research/01-delivery-and-deploy.md` 的「脚本必须保留的方法性结论」段（`run --rm` 而非 `exec`、不加 `--no-deps`、无版本时首次失败属预期）与「维护与回滚」段。

### `run.sh` — 起服务 + `/healthz` 探活

- **职责**：按「先迁移、再起服务」的固定顺序启动应用：默认先调 `migrate.sh`，`--skip-migrate`（或 `SKIP_MIGRATE=1`）可跳过；`docker compose up -d` 后轮询 `/healthz`，两者都完成才算启动成功。
- **用法要点**：`bash deploy/run.sh`；`--compose-file F` 同上；`HTTP_PORT` 环境变量覆盖 `.env` 的解析值（读不到退 8000）；健康检查默认 30 次 × 2s（总窗口 60s），`HEALTH_RETRIES` / `HEALTH_INTERVAL` 可覆盖；宿主无 `curl` 时降级为容器内 Python 标准库探测；失败时打印排查清单（含容器状态与日志尾部回显），并在末尾提醒脱敏（`AGENTS.md` §8 第 4 条：日志里不应出现邮件正文、收件人地址或凭据）。
- **启动前等价复检**：`up -d` 之前对应用镜像与 compose 依赖镜像**逐个**做 `docker image inspect`——必须本机存在（离线服务器不能去远端拉）且架构为 `linux/amd64`。这是 research 01 方法性结论「`docker load` 之后逐 tag `inspect` 复检架构」在消费侧的自动化等价物。
- **判据出处**：`docs/research/01-delivery-and-deploy.md` 的「脚本必须保留的方法性结论」段（`up -d` 返回 0 不等于可用、`.env` 端口解析容忍 CR / 引号 / 行尾注释、无 `curl` 时容器内探测）。

## 交付链运行前提：运行期零外网

- **运行期不得依赖任何外部网络**（`AGENTS.md` §8 第 5 条）：迁移与服务都在容器内完成，离线/隔离环境里 `uv run` 若去做隐式同步，必然去外网取包而 DNS 失败。**理由与修法的唯一出处**是 `docs/research/01-delivery-and-deploy.md` 对应条目与 `Dockerfile` 的 runtime `ENV UV_NO_SYNC=1`——因此两件 shell 脚本**刻意不重复写 `--no-sync`**，迁移命令保持本册已记录的字面形状。
- **归因**：`.venv` 不完整时的报错（`Failed to spawn` / `Failed to download` + dns error）按 `migrate.sh` 失败分类④判读；修复方向是回有网构建机重建镜像，不是在服务器上补装（`UV_NO_DEV=1` 不是修法，理由见本册条目）。

## 带入与载入（手动步骤，刻意不脚本化）

`package.sh` 只产出 tar 与 `.sha256` 旁路文件、并打印下面四步命令；**传输、载入与载入后复检由人手动执行**。四条均为**方法性结论**，出自 `docs/research/01-delivery-and-deploy.md` 的「脚本必须保留的方法性结论」段，此处只是使用说明、不是判据出处：

1. **传输**：可续传、不得改动二进制内容、文件名一律 ASCII、`.env` 单独传（宿主没有 `git`，只能靠 `rsync` / 归档）。
2. **校验完整性**：先 `cd` 到 tar 所在目录再跑 `sha256sum -c --status <tar>.sha256`（旁路文件格式 `"<hash>  <name>"` 两个空格 + LF）；**旁路文件缺失时打印实算 hash 供人工比对——这是降级路径，不是直接失败**。
3. **载入**：`docker load -i <tar>`（`save` / `load` 一律**不传** `--platform`——服务器 API 1.45 不支持该参数）。
4. **载入后复检架构**：按 `docker load` 输出里 `Loaded image: ` 前缀逐 tag 枚举，对每个 tag 执行 `docker image inspect --format '{{.Os}}/{{.Architecture}}' <tag>`，任一不是 `linux/amd64` 即失败。

第 4 步在消费侧已有自动化等价物：`run.sh` 启动前对应用镜像与 compose 依赖镜像逐个做同一强度的 `image inspect` 复检（见上），手动复检仍建议保留在载入当场，两者不互斥。

**为什么这四步不写成脚本**（所有者裁决）：脚本里就得写服务器地址、账号或凭据，而介质渠道与交付时机由所有者手工掌握；回滚同理——回滚动作只有两条命令，且第二条是否执行取决于「本次是否含破坏性迁移」，脚本判错会把库降级到与镜像不匹配的版本（`docs/research/01-delivery-and-deploy.md` 的「维护与回滚」段）。

## 边界

- 不要在此放应用代码。
- **不要擅自新建部署脚本**：交付链的形态与判据以 `docs/research/01-delivery-and-deploy.md` 为准；**带入与 `docker load` 刻意保持手动**，不要「顺手」把它脚本化——那会把服务器地址与凭据牵进仓库。
