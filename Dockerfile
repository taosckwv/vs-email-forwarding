# syntax=docker/dockerfile:1
# =============================================================================
# mail-relay 生产镜像
#
# 用途（见 docs/adr/0013）：镜像**在目标服务器（CentOS 8）上本地构建** ->
#       docker compose up -d；基础镜像以镜像 tar 形式从外部带入并 `docker load`。
# 环境前提：**与公网隔离、可达内网镜像源**——构建期可达内网 PyPI 源（装 wheel）、
#       宿主可达内网 yum 源；**运行时不得依赖任何外部网络**。
# 构建产物里 **不含** 任何需要联网才能启动的东西（见 AGENTS.md §8.5）。
#
# 需要 BuildKit（本文件用了 --mount=type=cache 与多阶段 ARG）：
#   Docker Desktop 默认开启；Linux 上可用 DOCKER_BUILDKIT=1 显式打开。
# =============================================================================

# --- 基础镜像 tag 的取法（两处都锁死，不用 latest、不用裸 slim） --------------
#
# python:3.13-slim-trixie
#   * 为什么 codename 必须写死：`python:3.13-slim` 是**浮动 tag**，它指向哪个
#     Debian 发行版毫无提示——
#       2025-07-23 的 3.13.5-slim = Debian 12 bookworm（glibc 2.36）
#       2025-08-14 的 3.13.6-slim = Debian 13 trixie  （glibc 2.41）
#     tag 名字一个字没变，glibc 从 2.36 跳到 2.41。
#   * 本项目是「镜像在目标服务器上本地构建，基础镜像以 tar 带入」（见
#     `docs/adr/0013`）：基础镜像 tar 由外部机器产出后带入，浮动 tag 意味着
#     今天带入的与下次带入的可能是两套 ABI，而现场无法判断差异从哪来、
#     更无法回退重建。所以 codename 必须钉死。
#   * 为什么不用 latest：latest 会在无通知的情况下升级 Python 小版本，
#     门禁跑过的解释器与生产跑的可能不是同一个。
#   * digest 需在固化前自行复核（2026-09-29 抓到的 amd64 digest 为
#     sha256:37134a49d21d2120e4c4d73bb76f8a4ab9aef31f096f7ec2ead48c2feead4332）
#   * 保守备选：python:3.13-slim-bookworm（Debian 12 / glibc 2.36）。若担心 trixie
#     上某些二进制轮子覆盖不全，用它是更稳的选择，但要在 ADR 里写明这是**有意的
#     保守选择**，而不是随手写的一个 tag。
#   * 为什么不用 alpine（python:3.13-alpine）：理由**不是**「没有 musl 轮子」——
#     已核实 psycopg-binary / asyncpg / greenlet 都有 cp313 musllinux_1_2 轮子。
#     真正的理由是两条：一是 musl 与生产宿主的 glibc 是两套 libc ABI，出问题时
#     排查面翻倍；二是 psycopg 官方明确说二进制包是 best-effort
#     （"Binary packages are produced on a best-effort basis"），离线环境里
#     一旦某个版本缺轮子就再也装不上（服务器无网、无法现场编译）。
#
# 镜像架构固定 linux/amd64，且**只在构建期钉死**：见 deploy/build-image.ps1 的
# -Platform（`docker build --platform linux/amd64`）与 compose 服务里的 platform 字段
# （后者只是运行期保险）。
#   * `docker save` / `docker load` **不带** `--platform`：该参数需要 API 1.48
#     （Engine v28），而生产服务器实测为 **26.1.3 / API 1.45**，传了会因未知参数直接失败。
#   * 平台改由「保存前校验 + 载入后校验」保证，并可用 `deploy/check-image-archive.py`
#     在交付前对归档做不依赖 Docker 的自检。理由与否决方案见 docs/adr/0006。
#
# ghcr.io/astral-sh/uv:0.12.20
#   * uv 官方容器镜像，0.12.20 是正式发布版本（非 pre-release），已核实该 tag
#     在 ghcr 上存在。这里只用它拷 /uv 与 /uvx 两个二进制，不把它当基础层。
ARG PYTHON_IMAGE=python:3.13-slim-trixie
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.20

# =============================================================================
# 阶段 0：uv 二进制来源（只提供 /uv 与 /uvx）
# =============================================================================
FROM ${UV_IMAGE} AS uv-source

# =============================================================================
# 阶段 1：builder —— 只负责把依赖装进 /app/.venv
# 为什么要多阶段：编译期产物与缓存（编译器、头文件、uv 下载缓存、wheel 构建中间
# 文件）都不该进运行镜像。运行镜像只保留 .venv + src，镜像更小、攻击面更小，
# 离线 tar 也更小（传输介质友好）。
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
#   离线交付要求「开发机装了什么，服务器就跑什么」，lock 一旦被就地改写，
#   交付物与门禁跑过的依赖就不是同一份了。
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
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# =============================================================================
# 阶段 2：runtime —— 只带运行需要的东西
# =============================================================================
FROM ${PYTHON_IMAGE} AS runtime

# 运行期环境变量：
#   PYTHONDONTWRITEBYTECODE / PYTHONUNBUFFERED：非 root 下不写 .pyc，日志实时刷出。
#   UV_FROZEN=1：运行期禁止 uv 动 lock，避免无网环境下尝试解析依赖。
#   UV_CACHE_DIR：uv 需要一个可写的缓存目录，指到 app 用户家目录下。
#   PATH：把 .venv/bin 放进 PATH，容器内 exec 直接用 python/alembic/uvicorn 也行。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
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

# uv 二进制留在运行镜像里，是**故意**的：
#   1) 离线服务器上要跑数据库迁移（deploy/migrate.sh ->
#      `uv run alembic upgrade head`），运行期没有网络，不能临时装工具；
#   2) 离线服务器上也不允许依赖宿主机装 Python 或 uv（见 deploy/README.md）；
#   3) `uv run` 会沿用构建期同步好的 .venv，不需要重新解析依赖。
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
CMD ["uv", "run", "uvicorn", "mail_relay.main:app", "--host", "0.0.0.0", "--port", "8000"]
