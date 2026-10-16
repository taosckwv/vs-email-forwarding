# syntax=docker/dockerfile:1
# =============================================================================
# vs-email-forwarding 生产镜像
#
# 用途（见 docs/research/01-delivery-and-deploy.md）：镜像**在开发机上构建**（deploy/build.sh）
#       -> 应用镜像以 tar 形式带入服务器并 `docker load`（deploy/package.sh）-> docker compose up -d。
#       **服务器不执行任何构建。**
# 环境前提：服务器**与公网隔离、可达内网镜像源**——宿主可达内网 yum 源；**运行时不得依赖
#       任何外部网络**。构建机是开发机，它**有公网**，构建期从 PyPI 装 wheel。
# 构建产物里 **不含** 任何需要联网才能启动的东西（见 AGENTS.md §8.5）。
#
# 需要 BuildKit（本文件用了 --mount=type=cache 与多阶段 ARG）：
#   构建在开发机（CentOS 8 / Docker 26.1.3）上进行，与生产**同版本、同 image store 模式**；
#   BuildKit 是该版本 Docker 的默认构建器，无需额外开关。
# =============================================================================

# --- 基础镜像 tag 的取法（两处都锁死，不用 latest、不用裸 slim） --------------
#
# python:3.13-slim-trixie
#   * 为什么 codename 必须写死：`python:3.13-slim` 是**浮动 tag**，它指向哪个
#     Debian 发行版毫无提示——
#       2025-07-23 的 3.13.5-slim = Debian 12 bookworm（glibc 2.36）
#       2025-08-14 的 3.13.6-slim = Debian 13 trixie  （glibc 2.41）
#     tag 名字一个字没变，glibc 从 2.36 跳到 2.41。
#   * 本项目在**开发机**上构建、应用镜像以 tar 带入（见
#     `docs/research/01-delivery-and-deploy.md`）：基础层在构建机上是浮动的——今天构建的与下次
#     构建的可能是两套 ABI，而现场无法判断差异从哪来、更无法回退重建。
#     所以 codename 必须钉死，并进一步按 digest 钉死（见下方 ARG）。
#   * 为什么不用 latest：latest 会在无通知的情况下升级 Python 小版本，
#     门禁跑过的解释器与生产跑的可能不是同一个。
#   * digest **按所有者裁决钉死**（见下方 ARG），2026-10-16 复核过一次读数：
#     amd64 的 manifest list digest = sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f。
#     代价：基础镜像的安全更新**不会自动进入**——升级时要人工复核新 digest、改这一行、重建镜像。
#   * 保守备选：python:3.13-slim-bookworm（Debian 12 / glibc 2.36）。若担心 trixie
#     上某些二进制轮子覆盖不全，用它是更稳的选择，但要在 `docs/research/01-delivery-and-deploy.md` 里写明这是**有意的
#     保守选择**，而不是随手写的一个 tag。
#   * 为什么不用 alpine（python:3.13-alpine）：理由**不是**「没有 musl 轮子」——
#     已核实 psycopg-binary / asyncpg / greenlet 都有 cp313 musllinux_1_2 轮子。
#     真正的理由是两条：一是 musl 与生产宿主的 glibc 是两套 libc ABI，出问题时
#     排查面翻倍；二是 psycopg 官方明确说二进制包是 best-effort
#     （"Binary packages are produced on a best-effort basis"），离线环境里
#     一旦某个版本缺轮子就要临时处置（构建机有公网、可随时重建镜像，但缺轮子仍意味着
#     要么换版本、要么就地编译，排查面会变大）。
#
# 镜像架构固定 linux/amd64，且**只在构建期钉死**：由产出侧构建脚本传
# `docker build --platform linux/amd64`（`deploy/build.sh`），compose 服务里的 platform
# 字段只是运行期保险。
#   * `docker save` / `docker load` **不带** `--platform`：该参数需要 API 1.48
#     （Engine v28），而生产服务器实测为 **26.1.3 / API 1.45**，传了会因未知参数直接失败。
#   * 平台改由「保存前校验 + 载入后校验」保证：镜像归档在进入服务器之前，必须先用**不依赖 Docker**
#     的工具自检（`deploy/check_archive.py`，由 `deploy/package.sh` 自动调用）——
#     单平台 linux/amd64、不含 image index / manifest list、不含 attestation。
#     理由与被否决方案见 docs/research/01-delivery-and-deploy.md。
#
# ghcr.io/astral-sh/uv:0.12.20
#   * uv 官方容器镜像，0.12.20 是正式发布版本（非 pre-release），已核实该 tag
#     在 ghcr 上存在。这里只用它拷 /uv 与 /uvx 两个二进制，不把它当基础层。
ARG PYTHON_IMAGE=python:3.13-slim-trixie@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.20@sha256:100047e74f30778ab704942321a09750d6158739573ff58bf3924085cc6cd2d8

# =============================================================================
# 阶段 0：uv 二进制来源（只提供 /uv 与 /uvx）
# =============================================================================
FROM ${UV_IMAGE} AS uv-source

# =============================================================================
# 阶段 1：builder —— 只负责把依赖装进 /app/.venv
# 为什么要多阶段：编译期产物与缓存（编译器、头文件、uv 下载缓存、wheel 构建中间
# 文件）都不该进运行镜像。运行镜像只保留 .venv + src，镜像更小、攻击面更小。
# 注意：应用镜像**就是外传介质**（每次发版都以 tar 带入服务器，见
# docs/research/01-delivery-and-deploy.md），而 python / uv 两个构建期基础镜像不再外传
# （构建发生在开发机）。因此「镜像更小」的理由包含**传输体积**与攻击面两项。
# =============================================================================
FROM ${PYTHON_IMAGE} AS builder

COPY --from=uv-source /uv /uvx /bin/

# uv 的标准环境变量（官方推荐的容器用法）：
#   UV_LINK_MODE=copy      : 容器层里不要硬链接。默认的硬链接会跨层/跨文件系统
#                            失效，导致 .venv 里的文件指向构建期的临时缓存。
#   UV_COMPILE_BYTECODE=1  : 构建期就把 .pyc 编好。运行期首启更快，而且运行期
#                            不必再往 __pycache__ 写文件（运行用户非 root，写不进去）。
#   UV_PYTHON_DOWNLOADS=0  : 禁止 uv 自动下载独立 Python。我们只用基础镜像自带的
#                            3.13；离线环境里一旦触发下载就是硬失败。
#   UV_PROJECT_ENVIRONMENT: 把虚拟环境固定在 /app/.venv，便于整目录拷到运行阶段。
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# --- 缓存分层：先依赖、后源码 ------------------------------------------------
# pyproject.toml / uv.lock 不变时，这一层的依赖安装结果可以整层复用；
# 只改 src/ 的日常迭代不会重装依赖。
COPY pyproject.toml uv.lock ./

# --frozen：严格按 uv.lock 安装，绝不解析/改写 lock。
#   构建在开发机上、依赖由版本库的 uv.lock 固定；lock 一旦被就地改写，
#   构建产出与门禁跑过的那一份依赖就不是同一份了。
# --no-install-project：本层还不拷 src，装不了项目本体，先只装第三方依赖。
# --no-dev：运行镜像不装 dev 依赖（ruff/mypy/pytest 等）。
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# 再拷源码与包元数据需要读的文件，然后安装项目本体。
# README.md：pyproject.toml 里 readme = "README.md"，构建元数据时必须存在。
# LICENSE  ：pyproject.toml 里 license-files = ["LICENSE"]，同理必须存在。
#   两个都显式 COPY：文件缺失就让构建在这一步直接失败，比 uv 的间接报错好定位。
#   （.dockerignore 里 `*.md` 被排除，但 `!README.md` 是反向例外，README 仍在上下文里。）
COPY README.md LICENSE ./
COPY src/ ./src/
# migrations/ 必须进镜像：数据库迁移是「在服务器上、在应用镜像里」跑的
#   （`docker compose run --rm app uv run alembic -c migrations/alembic.ini upgrade head`，
#    见 docs/research/01-delivery-and-deploy.md 的「维护与回滚」与「落地位置」段，
#    以及 deploy/migrate.sh）。alembic.ini 里的 script_location = migrations、
#    prepend_sys_path = . 都是**相对仓库根**的路径，所以容器内 CWD=/app 下必须同时有
#    migrations/alembic.ini、migrations/env.py 与 migrations/versions/。
#   离线服务器上不能临时安装工具、也不能指望宿主提供这些文件，镜像自带是唯一
#    能同时满足「不依赖宿主」与「回滚是改 tag 不重新构建」的做法（宿主挂载不随 tag 走）。
#   构建上下文本来就含 migrations/（.dockerignore 未排除它），这一行没有额外代价。
COPY migrations/ ./migrations/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# =============================================================================
# 阶段 2：runtime —— 只带运行需要的东西
# =============================================================================
FROM ${PYTHON_IMAGE} AS runtime

# 运行期环境变量：
#   PYTHONDONTWRITEBYTECODE / PYTHONUNBUFFERED：非 root 下不写 .pyc，日志实时刷出。
#   UV_NO_SYNC=1：**运行期禁止 uv 同步项目环境**（等价于每条 `uv run` 都带 `--no-sync`）。
#     为什么必须：镜像里的 .venv 是构建期 `uv sync --frozen --no-dev` 装的（只有运行依赖），
#     而 `uv run` 默认会按 uv.lock 同步**含 dev 组**的完整环境；于是容器内每条 `uv run`
#     —— 迁移命令 `uv run alembic …` 与默认 CMD `uv run uvicorn …` 都是 —— 都会尝试去外网取
#     ruff/mypy/pytest 等 dev 包。离线服务器上必然 DNS 失败：迁移报错、容器起不来，
#     撞 AGENTS.md §8.5「运行时不得依赖任何外部网络」。
#     这一行同时覆盖「迁移」与「服务」两条路径，并刻意**不动** docs/research/01
#     已记录的命令字面形状（`uv run alembic -c migrations/alembic.ini upgrade head`）。
#     反面提醒：`UV_NO_DEV=1` **不是**修法 —— 它只是少装 dev 组，uv 仍会去外网解析构建后端
#     （hatchling 等），离线照样失败；必须关掉同步本身。
#   UV_FROZEN=1：运行期禁止 uv 动 lock（避免无网环境下尝试重新解析依赖）。
#   UV_CACHE_DIR：uv 需要一个可写的缓存目录，指到 app 用户家目录下。
#   PATH：把 .venv/bin 放进 PATH，容器内 exec 直接用 python/alembic/uvicorn 也行。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_SYNC=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_FROZEN=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_CACHE_DIR=/home/app/.cache/uv \
    PATH="/app/.venv/bin:${PATH}" \
    HOME=/home/app

# 非 root 运行（uid/gid 10001）：
#   * 容器逃逸后的权限面更小；
#   * 应用不需要写镜像里的任何文件（PostgreSQL 数据是另一个容器的卷）；
#   * 显式 uid/gid 便于宿主机侧对挂载目录设权限（预演/排查时用得上）。
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/app \
       --shell /usr/sbin/nologin app \
    && mkdir -p /home/app/.cache/uv \
    && chown -R 10001:10001 /home/app

WORKDIR /app

# 从 builder 拷贝运行所需内容（--chown 保证 app 用户可读/可执行）
COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
COPY --from=builder --chown=10001:10001 /app/pyproject.toml /app/uv.lock /app/README.md /app/LICENSE ./
COPY --from=builder --chown=10001:10001 /app/src ./src
# migrations/ 同样从 builder 取（内容与构建上下文一致），归属 app 用户：
#   容器内 CWD=/app 时 `uv run alembic -c migrations/alembic.ini upgrade head` 才能找到
#   script_location = migrations 与 prepend_sys_path = .（见 builder 阶段的说明）。
COPY --from=builder --chown=10001:10001 /app/migrations ./migrations

# uv 二进制留在运行镜像里，是**故意**的：
#   1) 离线服务器上要跑数据库迁移（deploy/migrate.sh ->
#      `uv run alembic upgrade head`），运行期没有网络，不能临时装工具；
#   2) 离线服务器上也不允许依赖宿主机装 Python 或 uv（见 deploy/README.md）；
#   3) 运行镜像里的 .venv 就是**构建期同步好的那一份**：上面 `UV_NO_SYNC=1` 关掉了
#      `uv run` 的隐式同步，所以容器内 `uv run` 既不解析也不下载依赖，直接用既有 .venv。
#      —— 修正旧说法「uv run 会沿用构建期同步好的 .venv，不需要重新解析依赖」：
#      默认行为下它**会**重新解析/同步（含 dev 组），离线必失败；是本 ENV 让它不这么做。
#      若 .venv 因构建异常而不完整，`uv run` 会以「Failed to spawn / 无法创建环境」告警，
#      deploy/migrate.sh 的失败分类④ 会把这种情况点名到「构建期 uv sync 未生效」。
# 只拷两个二进制，不把构建期缓存带进来（体积代价约 30MB 量级）。
COPY --from=uv-source /uv /uvx /bin/

# 以数字 uid 启动，k8s/普通 docker 都能正确识别（不依赖镜像内的 /etc/passwd）
USER 10001:10001

# 容器内固定监听 8000；宿主机映射端口由 compose 的 ${HTTP_PORT} 决定。
# 若将来需要容器内也用别的端口，把 CMD 换成 shell 形式并用 ${HTTP_PORT:-8000}。
EXPOSE 8000

# 健康检查用 Python 标准库实现（slim 基础层里没有 curl/wget，不额外装包）。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3).status == 200 else 1)"]

# 用 `uv run` 而不是直接调 uvicorn：保证运行的就是 .venv 里锁定的那一份解释器与依赖。
CMD ["uv", "run", "uvicorn", "vs_email_forwarding.main:app", "--host", "0.0.0.0", "--port", "8000"]
