# scripts/ —— 本地开发与门禁脚本

本目录只放**本地开发与门禁**脚本。部署相关脚本（构建镜像、离线交付、迁移、启动）
全部在 `deploy/`，**不要在这里另起一套**（见 `AGENTS.md` §3、§4）。

所有脚本都从**任意工作目录**调用都能正确工作：bash 版用 `${BASH_SOURCE[0]}` 推导仓库根，然后切到仓库根再执行。

仓库**只保留 bash 脚本**：开发机是 Linux，Windows 版的 `check.ps1` / `dev-up.ps1` 已删除（见 `docs/research/05-dev-and-build-environment.md`）。

---

## 脚本清单

| 脚本 | 用途 | 参数 | 前置条件 |
|---|---|---|---|
| `check.sh` | **一键门禁**：依次跑 `ruff format --check` → `ruff check` → `mypy src` → `pytest`（带覆盖率）→ `mutmut run` **+ 逐条复验存活变异体**（第 5 步每次先清 `mutants/` 做全量变异，保证数字可复现） | 无（刻意不接受参数，保证所有人跑的是同一套门禁） | 已安装 **uv**（`uv --version` 可用）。**不需要 Docker**（单测、变异测试与存活复验都不依赖容器）。第 5 步实测总耗时约 30 秒 |
| `dev-up.sh` | 起开发依赖：PostgreSQL 17 + Mailpit（`compose.dev.yaml`），并打印连接信息 | 无 | 已安装并启动 **Docker**（Linux 上的 Docker 不需要硬件虚拟化） |

---

## 一键门禁：为什么它这么重要

**本项目不跑 CI**（见 `AGENTS.md` §9 与 `docs/research/03-dependencies-and-toolchain.md`）。没有任何自动兜底：
不会有人在 PR 上替你跑检查，配置错误也不会被流水线拦住。

所以：

> **提交前必须自己跑 `bash scripts/check.sh`，必须全绿。**

仓库只保留 bash 脚本，执行的步骤、顺序、失败行为一致：

1. `uv run ruff format --check .` —— 代码格式检查
2. `uv run ruff check .` —— 静态检查（lint）
3. `uv run mypy src` —— 类型检查
4. `uv run pytest --cov --cov-report=term-missing` —— 单元与集成测试 + 覆盖率输出
5. `uv run mutmut run` —— 变异测试（证明断言真的能失败），随后**逐条用全套 `tests/unit` 复验存活变异体**
   （脚本每次先删掉 `mutants/` 做**全量**变异，见「第 4、5 步的判据」末条）

任何一步失败立即以**非零退出码**结束，后续步骤不再执行；输出中有中文步骤提示
（`[1/5] 代码格式检查` 这类），失败时明确指出是哪一步。

### 第 4、5 步的判据

**覆盖率只作诊断**：第 4 步输出 `term-missing` 表，用来暴露「没有任何测试执行到」的行；**不设
`--cov-fail-under`**，覆盖率不作为验收门槛（理由见 `docs/research/03-dependencies-and-toolchain.md`）。

**变异测试卡结构性红线，不设百分比阈值**：

1. `mutmut` 本身运行失败；
2. 变异体总数为 0（配置失效，这一步等于没跑）；
3. 被杀数为 0 且存在存活变异（断言与实现完全同义反复——把实现改错，测试也不会红）。

**存活清单不采信 `mutmut` 自己的判定，必须逐条复验**：`mutmut` 不是对每个变异体跑全套测试，
它只按 `tests_by_mangled_function_name` 给每个变异体挑 1~2 条「相关」用例；它还把 pytest 的
退出码 2（收集/导入错误）映射成 `check was interrupted by user`，于是导入期就崩的变异体被
静默放过。所以第 5 步在 `mutmut run` 之后对**每个被判为存活的变异体**再跑一次全套
`tests/unit`（带 `--continue-on-collection-errors`），按复验退出码重新认定：**`0` ＝真存活，
非 `0` ＝杀死**。因此第 5 步还有两条红线：

4. `mutants/**/*.meta` 里出现既不是杀死也不是存活的退出码（尤其 2）—— 一律判失败并列出清单；
5. 复验出现无法解释的退出码、或复验的两条自检不成立（不设变异体时全套单测必须通过；
   `MUTANT_UNDER_TEST=fail` 必须非 0）—— 一律判失败。

**第 5 步每次都做全量变异（脚本自己先 `rm -rf mutants/`），不留缓存**：`mutmut` 的增量重跑只
重新生成它认为变过的文件，于是**同一份代码会给出不同的变异体条数**（实测见过 `done in 891ms
(2 files mutated, 0 ignored, 24 unmodified)` → 83 个变异体，以及同一份代码清缓存后 128 个），
门禁数字就不可复现——而数字不可复现的基线等于没有基线。代价实测只比增量多约 1 秒（瓶颈在
逐条复验，不在变异生成），所以这个代价值得付。**不要**为了省这一秒把清理去掉：`mutants/`
只是本次运行的中间产物，读数字的人必须能确定自己看到的是全量结果。

其余情况一律通过，并打印 **mutmut 原始判定**与**复验后真实判定**的 `killed / survived / total`、
复验中被改判为杀死的变异体清单、以及复验后真实存活的清单，**由人逐条评审**：

```bash
uv run mutmut results   # mutmut 视角的存活清单（未经复验，仅供参考）
uv run mutmut browse    # 逐条明细（可把变异体写回磁盘查看）
```

复验用的命令与 `check.sh` 里的一致（`mutants/` 里是变异源码副本，**必须让它先于仓库 `src/` 被 import**）：

```bash
env MUTANT_UNDER_TEST=<变异体名> PYTHONPATH="$PWD/mutants:$PWD/mutants/src" \
  uv run python -m pytest mutants/tests/unit -q -p no:randomly \
  --continue-on-collection-errors --rootdir=mutants
```

⚠️ 直接跑 `env MUTANT_UNDER_TEST=… uv run python -m pytest tests/unit` 会 import 仓库里**未被变异**的
`src/`，于是每条变异体都「通过」——这是最容易被骗的一步。`check.sh` 的复验因此自带两条对照：
不设变异体时全套必须通过（命令与环境没坏）、`MUTANT_UNDER_TEST=fail` 必须非 0（变异源码确实被加载）。

**存活的变异不是「必须清零」的指标**：要逐条判断它是**真漏测**、**等价变异**还是**已接受缺口**，
结论写进 PR。测试侧该写什么、怎么写见 `docs/testing.md`，三类归因的口径见
`docs/research/03-dependencies-and-toolchain.md`。

### 关于 docker 与门禁的关系

- 上面 5 步本身**不需要 Docker**：`tests/unit/` 是纯逻辑测试，第 5 步的变异测试与存活复验也只跑 `tests/unit`（范围写在 `pyproject.toml` 的 `[tool.mutmut]`）。
- `tests/integration/` 需要真实 PostgreSQL 与邮件服务，由 `scripts/dev-up.sh` 在本机起容器提供——**开发机已装 Docker，本机跑得了**（见 `docs/research/05-dev-and-build-environment.md`）；未起容器时按 pytest 的 `integration` 标记跳过（标记约定见 `docs/testing.md` §8）。
- 门禁脚本刻意不加"先起容器"的逻辑：让门禁的依赖尽量少，才能做到"随手就能跑"。**因此容器未起时集成用例会被跳过，而跳过不等于通过**——涉及数据库语义的验收要先 `bash scripts/dev-up.sh`。

---

## 开发依赖：`dev-up` 起的是什么

**这套开发依赖在开发机（CentOS 8 Linux）上起得来**——开发机已装 Docker，用 `bash scripts/dev-up.sh` 起（见 `docs/research/05-dev-and-build-environment.md`）。

`compose.dev.yaml` 里只有两个服务：

| 服务 | 端口 | 作用 |
|---|---|---|
| `postgres:17` | `127.0.0.1:5432` | 开发库 `vs_email_forwarding_dev`（测试库 `vs_email_forwarding_test` 需手工创建，命令见 `compose.dev.yaml` 末尾） |
| `axllent/mailpit` | `1025`(SMTP) / `8025`(Web UI) / `1110`(POP3，未映射到宿主) | 流②发信侧验证：应用发出去的信被它收下并可在 Web UI 查看。**它不提供 IMAP**，流③入站回路不能用它（见 `docs/open-questions.md` D16） |

Mailpit 的 Web UI：<http://127.0.0.1:8025>。

应用本体**不在容器里跑**，用 uv 在宿主机跑（热重载、调试都方便）：

```bash
uv sync
uv run uvicorn vs_email_forwarding.main:app --reload
```

停止依赖：

```bash
docker compose -f compose.dev.yaml down          # 保留数据
docker compose -f compose.dev.yaml down -v       # 连命名卷一起删（测试库/开发库全清）
```

---

## 常见问题

| 现象 | 处理 |
|---|---|
| `check.sh` 报「找不到 uv 命令」 | 先安装 uv（安装方式见 uv 官方文档），确认新开的终端里 `uv --version` 可用 |
| 第 5 步报「变异体全部存活」 | 断言与实现完全同义反复：按 `docs/testing.md` §1、§3.3 补**能失败**的断言；**不要**用 `# pragma: no mutate` 把存活抹掉 |
| 第 5 步报「复验后真实存活」比 `mutmut results` 少很多 | 正常现象：`mutmut` 只给每个变异体挑 1~2 条用例，会把其实会被全套单测杀死的变异体记成存活；复验后的数字才是真实判定。被改判的清单会一并打印出来 |
| 第 5 步报「mutmut 记录的退出码里有既不是杀死也不是存活的」 | 多半是 pytest 的收集/导入错误被 `mutmut` 当成了「用户中断」。门禁本身每次已是全量；若你是在手工跑 `mutmut`，先 `rm -rf mutants/` 再重跑。仍出现就按缺陷排查（导入期就崩的变异体必须人工处置，不得当成存活放过） |
| 第 5 步报「复验出现无法解释的退出码」或「复验环境自检未通过」 | 是复验环境本身有问题（不是变异体的问题）：按上一节的复验命令手工跑一次，确认它在容器未起时能跑通，再看 `mutants/src/` 是否是最新副本 |
| 第 5 步跑得很慢 | 第 5 步现在含逐条复验，实测约 30 秒，**且每次都做全量变异**（脚本自己先删 `mutants/`，为的是数字可复现）。增量与全量实测只差约 1 秒，不要为了省这一点去改清理逻辑 |
| 想知道自己看到的数字是不是全量结果 | 第 5 步开头会打印「清空 mutants/ 做全量变异」；门禁永远是全量。**手工**跑 `uv run mutmut run` 才是增量，那种数字不要拿去当基线 |
| 集成用例全部跳过、`pytest` 仍报绿 | 这是标记机制的正常行为，**不是通过**：先 `bash scripts/dev-up.sh` 再跑（`docs/testing.md` §6 第 7 条） |
| `dev-up.sh` 报「找不到 docker」 | 本机没装 Docker；按 `docs/research/05-dev-and-build-environment.md` 装（Linux 上的 Docker 不需要硬件虚拟化） |
| `dev-up.*` 报「连不上 Docker 守护进程」 | 两种原因：守护进程没启动（`systemctl start docker`），或**当前用户不在 `docker` 组**——socket 是 `root:docker 660`，且重新登录后组权限才生效 |
| `dev-up.sh` 报「permission denied ... docker.sock」 | 同上：把用户加进 `docker` 组（`usermod -aG docker <用户>`）后**重新登录** |
| 端口 5432 / 1025 / 1143 / 8025 被占用 | 先 `docker compose -f compose.dev.yaml down`；仍冲突则改 `compose.dev.yaml` 里的宿主端口映射 |
| `bash scripts/*.sh` 报 `$'\r': command not found` | 脚本被转成了 CRLF。`sed -i 's/\r$//' scripts/*.sh`，并检查 git 的 `core.autocrlf` 设置（这批脚本应以 LF 提交） |
| 脚本在仓库子目录里执行报路径错 | 不该发生（脚本自己推导仓库根）；若发生请当成 bug 反馈，不要手工 `cd` 绕过 |
