# 03 依赖与工具链

> **本册是「技术栈、依赖清单与本地门禁」的当前有效结论真源。**
> 只写：当前结论、当前实测证据、已否决方向、落地位置、未定项。
> **具体依赖的选型理由、代价与许可实测**见 `specs/0001-email-forwarding-service/design.md`（本册只记依赖清单、放行程序与工具链）。

## 结论

**技术栈**

- **Python 3.13 是目标版本**：`.python-version` 锁定 `3.13`，`pyproject.toml` 的 `requires-python` 为 `>=3.13,<3.14`，`uv.lock` 记录的解析约束是 `==3.13.*`——三处一致，`.python-version` 不得改动。
- **依赖与解释器管理一律用 `uv`**：由 uv 安装独立的官方 CPython 解释器（不依赖系统 Python）、创建虚拟环境、解析依赖并维护锁文件 `uv.lock`、管理依赖分组。一条 `uv sync` 就对齐解释器与全部依赖，队友克隆后不需要手工建 venv。`uv.lock` 是解析与锁定结果的唯一真源，随依赖变更一起提交。
- **Web 框架 FastAPI + uvicorn**；**配置用 pydantic-settings**（唯一配置入口，读 `.env`）；**日志用 structlog**；**测试用 pytest**（配 `pytest-asyncio`，`asyncio_mode = "auto"`）。
- **运行时依赖保持最小集**：`fastapi`、`uvicorn[standard]`、`pydantic-settings`、`structlog` 四项，加上数据与邮件访问四项——`sqlalchemy`、`alembic`、`asyncpg`、`IMAPClient`（**选型理由、代价与许可见 `specs/0001-email-forwarding-service/design.md` 的「依赖选型」与「实测证据」**）。开发依赖另在 `[dependency-groups] dev` 里：`ruff`、`mypy`、`pytest`、`pytest-asyncio`、`httpx`、`pre-commit`、`commitizen`。`httpx` 是 FastAPI `TestClient` 的必需依赖，**不是运行时依赖**。
- **新增运行时依赖前先在本册留下结论再动手**，不允许「先装上再补记录」。

**为什么是 3.13**

- **3.12 不采用**：已进入 security-only，只剩安全修复，剩余寿命比 3.13 更短。
- **3.14 不采用**：所有者对其稳定性不放心——生态适配与稳定期都还观察不足。
- **3.13 的分支状态**：处于 bugfix 阶段，其后只剩安全修复；3.14 已发布。**3.13 已过 bugfix 峰值，未来需要一次迁移到更高分支**；迁移成本主要落在依赖轮子与类型检查的目标版本上。
- **评估时机**：待 3.14 的生态与稳定性更明朗后再评估迁移，不在本阶段预设时间点。
- **兼容性论证只保留仍然成立的部分**：`httpx` 在 Python 3.13 上**实际可用**（本项目测试已跑通），因此任何以「某依赖未声明支持更高版本」为由的反对意见都不成立；真正支撑「3.13 为目标版本」的是上面两条（3.12 寿命更短、3.14 稳定性未获认可），不是某个库的版本声明。**不得再用依赖版本声明当作版本选型的论据**——依赖集对 Python 小版本变化并不敏感，这类论据本身自相矛盾。

**本机环境事实**

- 开发机（CentOS 8 Linux 虚拟机）的 `python3` 已是 **3.13**，与项目锁定的 `.python-version` 一致；但**一律用 `uv run` 执行**（`uv run ruff` / `uv run mypy` / `uv run pytest`），**不要直接调用 `python`**——让解释器与依赖统一由 uv 管理。
- **`uvloop` 在 Windows 上无 wheel**，`uvicorn[standard]` 在 Windows 上不会安装它；生产与开发机都是 Linux，可正常获得 uvloop 与服务端性能优化。
- 本机**不需要 Docker 就能跑完门禁**：门禁四步与 `tests/unit/` 都是纯逻辑，不依赖容器。开发依赖容器另由 `scripts/dev-up.sh` 起（见 `docs/research/05-dev-and-build-environment.md`）。

**门禁（不跑 CI）**

- **不跑 CI**：`.github/` 下只有 PR 模板与 Issue 模板，**不建 `workflows/`**，也不要新建任何 CI 配置。
- **门禁全部落在本地，没有任何自动兜底**：没有人会在 PR 上替你跑检查，配置错误也不会被流水线拦住。提交前必须自己跑一键脚本并全绿。
- **门禁分两层**：
  1. **pre-commit**——ruff format、ruff lint、commitizen 校验提交信息格式，外加基础文件卫生（行尾空白、文件末尾换行、YAML / TOML 可解析、合并冲突标记、大文件、行尾统一为 LF）。
  2. **一键脚本**——`scripts/check.sh`，固定四步、顺序不可调整：
     `uv run ruff format --check .` → `uv run ruff check .` → `uv run mypy src` → `uv run pytest`。
- 一键脚本的两个刻意设计：**不接受参数**（避免「本地跑的参数与别人不同」导致门禁不可比），**不加「先起容器」逻辑**（让门禁依赖尽量少，才能随手就跑）。任一步失败立即以非零退出码结束，后续步骤不再执行。
- **已知代价**：`git commit --no-verify` 可以跳过 pre-commit，且没有任何远端机制拦截；「这个改动跑过测试了吗」只能靠信任。这是接受不跑 CI 的直接代价，不另做补偿机制。
- **标准不得为了变绿而放宽**：mypy 要求完整类型标注、不允许 `# type: ignore` 兜底；`mypy src` 是门禁的一部分。某个库确实缺类型存根时，处置是先在册里留下结论，再在 `[[tool.mypy.overrides]]` 中按 module 精确放行，**不得整体关闭**。pytest 的标准同样不放宽。
- **不跑 CI 的代价之一是「没有自动化的兼容性验证」**：原本可以在 CI 里加一个「在目标发行版容器里安装依赖」的任务来提前暴露问题，这条路不存在了；因此部署链的兼容性只能靠**人工预演**兜底（预演环境的判定见 `docs/research/01-delivery-and-deploy.md`）。

**版本与 CHANGELOG（手工维护）**

- **版本号与 CHANGELOG 由人工维护**，版本号遵循语义化版本，发版时用 commitizen 辅助。
- **commitizen 只做提交信息格式校验**（Conventional Commits，描述用中文），在 `commit-msg` 阶段由 pre-commit 调用。
- **`python-semantic-release` 不采用**：它按提交历史自动打 tag、生成 CHANGELOG 与递增版本，设计前提是跑在 CI 里；本项目不跑 CI，它没有执行环境，收益接近零。
- `pyproject.toml` 的 `[tool.commitizen]` 把版本号与 `version_files = ["pyproject.toml:version"]` 关联，tag 格式为 `v$version`——**版本号的唯一落点是 `pyproject.toml` 的 `[project] version`**，其他地方不另写一份版本号。

**镜像版本标签约定**

- 镜像 tag 取自 `pyproject.toml` 的 `[project] version`；同一次构建**同时打两个 tag**：`<名称>:<版本>` 与 `<名称>:latest`。
- `<名称>:latest` 只是构建机上的便利 tag；**交付与回滚一律指向具体版本 tag**，不要依赖 `latest`。
- 版本号因此在三处保持同源：`pyproject.toml` 的 `[project] version`、commitizen 的 `version_files`、镜像的版本 tag。

**ruff 的规则取舍**

- 基础设置：`line-length = 100`、`target-version = "py313"`、`src = ["src", "tests"]`、排除 `migrations/versions`；`select` 为 `E` / `F` / `I` / `B` / `UP` / `SIM` / `RUF`。
- `ignore` 里**只有两处**：`E501`（行长交给 ruff format 负责，避免格式化器与 lint 互相打架），以及 `RUF001` / `RUF002` / `RUF003`。**其余规则一律不放宽**。
- `RUF001` / `RUF002` / `RUF003` 显式 ignore 的论证：
  1. 这三条命中的是**中文标点的全角字符**（`：`、`；`、`，`、`（`、`）` 等），与「代码质量问题」**不是同一性质的东西**：告警全部来自按项目要求写的中文注释、docstring 与面向用户的中文描述字符串，不是逻辑缺陷。
  2. 该例外是**全局的**（写在 `[tool.ruff.lint] ignore` 里，不区分文件与路径），因此**无法只对注释放宽而保留对标识符与字面量的检查**——工具层面不存在这个中间档。
  3. **代价**：全角字符一旦进入标识符或字符串字面量，门禁不再自动发现。缓解办法是**代码评审时人工留意标识符与字面量中的全角字符**；其余 RUF 规则（例如检查无用 `noqa` 的那条）与全部其他规则仍然生效。
  4. **触发条件式的再评估路径**（触发条件：真的出现全角字符混进标识符或字面量这类**真问题**）：届时改用 `per-file-ignores` 做精确限定，并**在本册单独记录这次决策的结论与代价**，而不是继续维持全局忽略——它**不是**本册当前的备选方案。
  5. **这不是「为了过门禁而放宽配置」**：被禁止的情形是「代码质量不达标，为了让门禁变绿而降低标准」；本条是「正文强制中文标点」这一既定写作规范与「规则假设 ASCII 标点」这一工具前提**相互冲突**，正确处置是**显式记录例外**，而不是反过来改代码去迎合工具。`AGENTS.md` 一方面禁止为过门禁放宽 ruff / mypy 配置，另一方面要求此类改动必须有据可查、有理由——本册记的就是那份依据。**该例外既不顺带忽略任何其他规则，也不降低 mypy 与 pytest 的标准。**
- 已否决的替代做法见「已否决方向」：改正文标点为 ASCII、把这三条降级为 warning、关闭整个 RUF 规则族、**现在就**把例外改成 `per-file-ignores` 逐文件列举，均不成立。

**构建环境事实**

- `docker build --platform` 的平台语义由 **BuildKit** 保证；把 `DOCKER_BUILDKIT` 设为 `0` 会关掉 BuildKit，`--platform` 的语义随之失效，可能静默产出错架构镜像。
- 因此**构建脚本必须显式拦住 `DOCKER_BUILDKIT=0`**：检测到就立即失败并说明原因，**不得静默降级或继续构建**。（构建脚本尚未实现，见未定项 D2。）

**生产环境的依赖前提**

- 生产环境**与公网隔离、可达内网镜像源**；构建期可达内网 PyPI 源、能正常安装 wheel，因此**不需要额外交付离线 wheel 包**，宿主机也有 `python3` 可临时排障——这一条已关闭，不属未定项。
- 运行时的隔离约束见 `docs/research/01-delivery-and-deploy.md`。

## 实测证据

- **门禁四项全绿**（在当前工作副本上逐一执行，命令入口即 `scripts/check.sh` 的四个步骤）：
  - `ruff format --check .` → `58 files already formatted`；
  - `ruff check .` → 零告警，退出码 0；
  - `mypy src` → `Success: no issues found in 15 source files`；
  - `pytest` → `1 passed`。
- **`ruff check` 的全部告警只来自 `RUF001` / `RUF002` / `RUF003`，零处来自其他规则**。把这三条从 `ignore` 中移出后复现（`ruff check . --select RUF001,RUF002,RUF003 --statistics`）：共 **314 处**，其中 `RUF002` 191 处、`RUF001` 92 处、`RUF003` 31 处，**没有第四种规则出现**。当前 `ignore` 生效时同一份代码报 0 处，且没有冒出任何其他规则的告警——这正是「只有中文标点这一处例外，其余标准未动」的直接证据。
- 同一次 `pytest` 还输出一条 `StarletteDeprecationWarning`：`Using httpx with starlette.testclient is deprecated; install httpx2 instead`，来源是虚拟环境中的 `fastapi/testclient.py`。它说明**测试是跑通的**（`httpx` 在 3.13 上实际可用），风险性质是「将来会不会被上游弃用」，已登记为未定项 E2。
- **`uvloop` 在 Windows 上确实不装**：`uv.lock` 中该依赖带 `sys_platform != 'win32'` 标记；开发机（Linux）实测已装 `uvloop 0.22.1`，生产同为 Linux，同样可获得。
- **解释器与依赖一律交给 uv 管理**：虚拟环境中的解释器实测为 **Python 3.13.16**，与 `.python-version` 的 `3.13` 一致；「一律用 `uv run`」是为了让解释器与依赖只由一处决定，而不是因为存在版本不一致。
- **`.github/` 下没有 `workflows/`**：实测只有 `ISSUE_TEMPLATE/`（`bug_report.md`、`feature_request.md`）与 `PULL_REQUEST_TEMPLATE.md`。
- **工具链与依赖的实测版本**（取自当前虚拟环境）：ruff 0.16.9、mypy 2.3.1、pytest 9.1.1、commitizen 4.19.0、pre-commit 4.6.2；运行时 fastapi 0.141.1、uvicorn 0.54.0、pydantic-settings 2.15.0、structlog 26.1.0；测试依赖 httpx 0.28.1、starlette 1.7.0。
- **门禁全绿的适用范围**：业务逻辑尚未实现，`src/` 下只有包占位与一个能返回 `/healthz` 的最小入口。因此上述绿只证明**工程配置与工具链可用**，不证明任何业务正确性。
- **依赖许可与类型标注的实测结果**见 `specs/0001-email-forwarding-service/design.md` 的「实测证据」一节（本册不复述）。

## 已否决方向

- Python 3.12 —— 原因：已进入 security-only，剩余寿命更短。
- Python 3.14 —— 原因：所有者对其稳定性不放心（生态与稳定期观察不足）。
- Java / Go / Node —— 原因：本项目是内部小体量服务，团队与生态选定 Python。
- poetry / pip-tools / requirements.txt —— 原因：`uv` 同时管解释器、虚拟环境与锁文件，一套工具覆盖。
- GitHub Actions 或任何 CI —— 原因：项目为私有仓库、无合规要求，门禁靠 pre-commit + 一键脚本已足够（代价：无远端拦截）。
- 只靠文档约定、不引入任何工具 —— 原因：门禁会退化成口头约定，没人执行也无法核对；pre-commit + 一键脚本至少是**可执行**的底线。
- 只写脚本、不安装 git hook —— 原因：脚本不会被自动触发，提交时仍然可以绕过；hook 让门禁在提交那一刻生效。
- `python-semantic-release` —— 原因：不跑 CI，自动发版没有执行环境；版本与 CHANGELOG 手工维护。
- 把正文改成 ASCII 标点以规避 RUF001/002/003 —— 原因：与「正文全中文」的写作规范冲突，代价大于收益。
- 降级 RUF001/002/003 为 warning —— 原因：ruff 没有 warning 级别。
- 关掉整个 RUF 规则族 —— 原因：RUF 族还有其他有价值的规则，不能整族关闭。
- **现在就**把例外改成 `per-file-ignores` 逐文件列举 —— 原因：中文标点在这类文件里**分布极广**，逐文件列举的维护成本高于收益，也不会比全局例外更安全。若将来真的出现全角字符混进标识符或字面量这类**真问题**，再按本册「结论」里那条路径重新评估——届时属于新的决策，要按 `AGENTS.md` §6 重写本册。
- 为无类型库整体关闭 mypy 检查 —— 原因：本册既定处置是**按 module 精确放行并在册里留下结论**，整体关闭等于降低门禁标准（既有结论的重申）。

## 落地位置

- `pyproject.toml` —— 运行时依赖清单与 `[dependency-groups] dev` 分组、`requires-python`、`[project] version`，以及 ruff / mypy / pytest / commitizen 的全部配置；`RUF001` / `RUF002` / `RUF003` 的 `ignore` 与理由注释就在此处。**新增的四项运行时依赖**（`sqlalchemy`、`alembic`、`asyncpg`、`IMAPClient`）与**针对无类型库的 `[[tool.mypy.overrides]]` 按 module 放行**（连同理由）也落在本文件；**这些依赖的选型理由与代价**见 `specs/0001-email-forwarding-service/design.md`。
- `.python-version` —— 锁定 3.13（不得改动）。
- `uv.lock` —— 依赖解析与锁定的唯一真源，随依赖变更一起提交。
- `.pre-commit-config.yaml` —— pre-commit 挂钩清单与 `commit-msg` 阶段；启用命令为 `uv run pre-commit install --install-hooks` 与 `uv run pre-commit install --hook-type commit-msg`。
- `scripts/check.sh` —— 一键门禁的唯一入口（必须 LF、无 BOM）；Windows 版 `check.ps1` 已删除。
- `scripts/README.md` —— 脚本清单、门禁为什么重要、与 Docker 的关系、常见问题处置。
- `tests/unit/` —— 不依赖数据库与网络的纯逻辑单测，边界写在 `tests/unit/README.md`；需要真实 PostgreSQL 与邮件服务的用例放 `tests/integration/`。
- `docs/open-questions.md` —— 未定项的唯一清单（本册「未定项」段只放指针）。
- `deploy/` —— 构建与交付脚本的落点；`DOCKER_BUILDKIT=0` 的显式拦截必须落在其中的构建脚本里（脚本尚未实现，见未定项 D2）。
- `AGENTS.md` —— §3 命令入口、§7 编码约定、§9 提交与 PR、§10 不要做的事，与本册互为规则与结论的分工。

## 未定项

以下问题**尚未决定**，详见 `docs/open-questions.md`；实现时**不得替它们挑一个「合理的默认值」**：

- E2 `httpx` 与 `httpx2` 的迁移时机（当前测试会打 StarletteDeprecationWarning，提示改用 httpx2）
- D1 各基础镜像 tag 的 digest 需在写进脚本前复核
- D2 部署脚本的具体形态（尚未实现）
