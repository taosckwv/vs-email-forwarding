# deploy/ — 部署脚本与交付链的落点

本目录放**服务器侧部署与交付链脚本**：数据库迁移入口、服务启动与健康检查、交付前归档自检；不放应用代码（那在 `src/`）。判据与硬约束的唯一真源是 `docs/research/01-delivery-and-deploy.md`，本文件只写职责、用法要点与判据出处，不复述其结论。

## 已落地的三件脚本

### `migrate.sh` — 数据库迁移入口（部署时执行 Alembic 的唯一入口）

- **职责**：在交付目录里用应用镜像跑一次性容器，执行 `docker compose run --rm app uv run alembic -c migrations/alembic.ini upgrade head`。命令形状固定在脚本内一处，不做第二入口。
- **用法要点**：`bash deploy/migrate.sh`；`--print-command` 只打印命令不执行；`--compose-file F`（或环境变量 `COMPOSE_FILE`）指定 compose 文件，默认取脚本上一级的 `compose.yaml`；`--skip-probe` / `SKIP_PROBE=1` 跳过「镜像内是否有 `migrations/`」预检。
- **失败分类（四类）**：① 结构性故障（镜像/工作目录里没有 `migrations/`，含 `alembic.ini` / `env.py` 缺件，有只读预检）；② 连接/库级故障；③ 迁移脚本自身错误（`upgrade head` 幂等，重跑安全）；④ 运行环境故障——镜像构建期 `uv sync` 未生效（`.venv` 不完整）。①②③ 与「※ 无版本判别式」在 `--help` 里；④ 只在失败输出里打印（脚本头注也指向它）。
- **判据出处**：`docs/research/01-delivery-and-deploy.md` 的「待实现脚本必须保留的方法性结论」段（`run --rm` 而非 `exec`、不加 `--no-deps`、无版本时首次失败属预期）与「维护与回滚」段。

### `run.sh` — 起服务 + `/healthz` 探活

- **职责**：按「先迁移、再起服务」的固定顺序启动应用：默认先调 `migrate.sh`，`--skip-migrate`（或 `SKIP_MIGRATE=1`）可跳过；`docker compose up -d` 后轮询 `/healthz`，两者都完成才算启动成功。
- **用法要点**：`bash deploy/run.sh`；`--compose-file F` 同上；`HTTP_PORT` 环境变量覆盖 `.env` 的解析值（读不到退 8000）；健康检查默认 30 次 × 2s（总窗口 60s），`HEALTH_RETRIES` / `HEALTH_INTERVAL` 可覆盖；宿主无 `curl` 时降级为容器内 Python 标准库探测；失败时打印排查清单（含容器状态与日志尾部回显），并在末尾提醒脱敏（`AGENTS.md` §8 第 4 条：日志里不应出现邮件正文、收件人地址或凭据）。
- **启动前等价复检**：`up -d` 之前对应用镜像与 compose 依赖镜像**逐个**做 `docker image inspect`——必须本机存在（离线服务器不能去远端拉）且架构为 `linux/amd64`。这是 research 01 方法性结论「`docker load` 之后逐 tag `inspect` 复检架构」在消费侧的自动化等价物。
- **判据出处**：`docs/research/01-delivery-and-deploy.md` 的「待实现脚本必须保留的方法性结论」段（`up -d` 返回 0 不等于可用、`.env` 端口解析容忍 CR / 引号 / 行尾注释、无 `curl` 时容器内探测）。

### `check_archive.py` — 交付前归档自检

- **职责**：只读检查 `docker save` 产出的基础镜像 tar 是否为**单平台 `linux/amd64`** 归档；**不依赖 Docker、只用 Python 标准库**，在 tar 进入服务器之前执行。
- **用法要点**：`python3 deploy/check_archive.py <tar>`；`--expected-images N` 显式指定期望基础镜像个数（默认 **3**：python 基础层 + uv 构建层 + `postgres:17` 运行期）；支持 OCI（`index.json`）与 legacy docker-archive（`manifest.json`）双布局；退出码 `0` 通过 / `1` 归档不合格 / `2` 用法或读取错误；**必须在生成 `.sha256` 旁路文件之前跑**——自检不通过就不要生成旁路文件、更不要传上去。
- **判据出处**：`docs/research/01-delivery-and-deploy.md` 的「待实现脚本必须保留的方法性结论」段（单平台归档、双布局、期望个数显式、旁路文件时序、退出码语义）。

## 交付链运行前提：运行期零外网

- **运行期不得依赖任何外部网络**（`AGENTS.md` §8 第 5 条）：迁移与服务都在容器内完成，离线/隔离环境里 `uv run` 若去做隐式同步，必然去外网取包而 DNS 失败。**理由与修法的唯一出处**是 `docs/research/01-delivery-and-deploy.md` 对应条目与 `Dockerfile` 的 runtime `ENV UV_NO_SYNC=1`——因此两件 shell 脚本**刻意不重复写 `--no-sync`**，迁移命令保持本册已记录的字面形状。
- **归因**：`.venv` 不完整时的报错（`Failed to spawn` / `Failed to download` + dns error）按 `migrate.sh` 失败分类④判读；修复方向是回有网构建机重建镜像，不是在服务器上补装（`UV_NO_DEV=1` 不是修法，理由见本册条目）。

## 手动载入与校验步骤（D2-乙三条方法结论；产出方打包脚本随 D13 推迟，当前只能手动做）

基础镜像 tar 的导出、带入与 `docker load` 尚无脚本（形态未定，与 D13 同题，见 `docs/open-questions.md`）。在此期间按以下三步手动执行；三条均为**方法性结论**，出自 `docs/research/01-delivery-and-deploy.md` 的「待实现脚本必须保留的方法性结论」段，此处只是使用说明、不是判据出处：

1. **`docker load -i <tar>`** 载入基础镜像 tar（`save` / `load` 一律**不传** `--platform`——服务器 API 1.45 不支持该参数）。
2. **校验传输完整性**：先 `cd` 到 tar 所在目录再跑 `sha256sum -c --status <tar>.sha256`（旁路文件格式 `"<hash>  <name>"` 两个空格 + LF）；**旁路文件缺失时打印实算 hash 供人工比对——这是降级路径，不是直接失败**。
3. **载入后复检架构**：按 `docker load` 输出里 `Loaded image: ` 前缀逐 tag 枚举，对每个 tag 执行 `docker image inspect --format '{{.Os}}/{{.Architecture}}' <tag>`，任一不是 `linux/amd64` 即失败。

第 3 步在消费侧已有自动化等价物：`run.sh` 启动前对应用镜像与 compose 依赖镜像逐个做同一强度的 `image inspect` 复检（见上），手动复检仍建议保留在载入当场，两者不互斥。

## 边界

- 不要在此放应用代码。
- **不要擅自新建部署脚本**：仍未实现的镜像构建脚本、基础镜像 tar 的导出与带入（含 sha256 旁路文件）、产出方打包脚本，其整体形态是未定项（`docs/open-questions.md` D2 / D13），经所有者拍板后再落。
- 判断标准、服务器侧硬约束与所有已否决方案见 `docs/research/01-delivery-and-deploy.md`。
