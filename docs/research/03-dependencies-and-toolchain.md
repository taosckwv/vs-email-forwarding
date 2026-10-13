# 03 依赖与工具链

> **本册是「技术栈、依赖清单与本地门禁」的当前有效结论真源。**
> 只写：当前结论、当前实测证据、已否决方向、落地位置、未定项。
> **具体依赖的选型理由、代价与许可实测**见 `specs/0001-email-forwarding-service/design.md`（本册只记依赖清单、放行程序与工具链）。

## 结论

**技术栈**

- **Python 3.13 是目标版本**：`.python-version` 锁定 `3.13`，`pyproject.toml` 的 `requires-python` 为 `>=3.13,<3.14`，`uv.lock` 记录的解析约束是 `==3.13.*`——三处一致，`.python-version` 不得改动。
- **依赖与解释器管理一律用 `uv`**：由 uv 安装独立的官方 CPython 解释器（不依赖系统 Python）、创建虚拟环境、解析依赖并维护锁文件 `uv.lock`、管理依赖分组。一条 `uv sync` 就对齐解释器与全部依赖，队友克隆后不需要手工建 venv。`uv.lock` 是解析与锁定结果的唯一真源，随依赖变更一起提交。
- **Web 框架 FastAPI + uvicorn**；**配置用 pydantic-settings**（唯一配置入口，读 `.env`）；**日志用 structlog**；**测试用 pytest**（配 `pytest-asyncio`，`asyncio_mode = "auto"`）。
- **运行时依赖保持最小集**：`fastapi`、`uvicorn[standard]`、`pydantic-settings`、`structlog` 四项，加上数据与邮件访问四项——`sqlalchemy[asyncio]`、`alembic`、`asyncpg`、`IMAPClient`（**选型理由、代价与许可见 `specs/0001-email-forwarding-service/design.md` 的「依赖选型」与「实测证据」**）。开发依赖另在 `[dependency-groups] dev` 里：`ruff`、`mypy`、`pytest`、`pytest-asyncio`、`httpx`、`pre-commit`、`commitizen`、`pytest-cov`、`mutmut`。`httpx` 是 FastAPI `TestClient` 的必需依赖，**不是运行时依赖**；`pytest-cov` 与 `mutmut` 是**测试质量的度量工具**，同样不是运行时依赖。
- **`sqlalchemy` 启用 `[asyncio]` extra**（`pyproject.toml` 里写 `sqlalchemy[asyncio]`）：异步引擎与 `AsyncSession` 需要 `greenlet`，而 `greenlet` 是 **SQLAlchemy 自己为 `asyncio` extra 声明的依赖**（`Requires-Dist: greenlet>=1; extra == "asyncio"`）——**不是新上游**，版本约束也留在上游一处。**不手工往 `pyproject.toml` 里加 `greenlet`**（理由见「已否决方向」），**也不靠「缺了再装」兜底**：缺它时 `import sqlalchemy.ext.asyncio` 直接报错，异步引擎根本建不起来。实测证据见「实测证据」。
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
- 本机**不需要 Docker 就能跑完门禁**：门禁五步与 `tests/unit/` 都是纯逻辑，不依赖容器——第 5 步的变异测试与存活复验同样只跑 `tests/unit`。开发依赖容器另由 `scripts/dev-up.sh` 起（见 `docs/research/05-dev-and-build-environment.md`）。

**测试质量的度量口径（门禁第 4、5 步的依据）**

- **覆盖率只作诊断，不作门槛**：第 4 步用 `pytest-cov` 输出 `term-missing`，作用是暴露「没有任何测试执行到」的行；**不设 `--cov-fail-under`**。覆盖率衡量的是执行，不是验证，单靠它会系统性高估信心。
- **断言质量用变异测试度量**：第 5 步用 `mutmut` 向被测代码注入人工缺陷，看测试能不能杀死它们。**判据是结构性红线，不是百分比阈值**——原有的三种失败情况是：① 工具运行失败；② 变异体总数为 0（配置失效，门禁形同虚设）；③ 被杀数为 0 且存在存活变异（断言与实现完全同义反复）——这三条只加强、不放宽。判据的完整清单与落地写法在 `scripts/check.sh`，其依据全在本节。其余一律通过，并输出 mutmut 原始与复验后的 killed / survived / total 与存活清单，**存活变异逐条交人工评审**。
- **不设变异分数阈值**：一个数字一旦成为目标就会被反向优化（写同义反复测试、滥用 `# pragma: no mutate` 把存活抹掉），而且本阶段没有任何实测依据支撑某个具体数字。
- **存活清单必须经复验才可采信**：`mutmut` 写的存活判定有两处不能直接信，因此门禁对每个判为「存活」的变异体**用全套 `tests/unit` 再跑一次**（`--continue-on-collection-errors`，与变异运行同样只跑单测），按复验退出码重新认定——`0` ＝真存活，非 `0` ＝杀死；复验自带两条对照（不设变异体时全套必须通过、`MUTANT_UNDER_TEST=fail` 必须非 0），挡住「复验其实没加载变异源码」的假象。两处坑：
  1. **mutmut 不是对每个变异体跑全套测试**，它按 `mutants/mutmut-stats.json` 的 `tests_by_mangled_function_name` 给每个变异体只挑 1~2 条它认为相关的用例。实测：`db.models` 的 `x__enum_check`、`x__nullable_enum_check` 名下各只有 1 条用例（`tests/unit/test_db_session.py::test_importing_the_module_does_not_create_an_engine`）——`CHECK` 约束在**模块导入期**构造，被归因给了第一条跑起来的用例。于是这些变异体在 mutmut 眼里「存活」，而全套单测跑起来立刻红：复验把 17 条 `db/models` 变异体从「存活」改判为「杀死」。
  2. **退出码 2 被当成「用户中断」**：`mutmut/stats.py` 的 `status_by_exit_code` 把 pytest 的退出码 2（收集/导入错误）映射成 `check was interrupted by user`，既不算杀死也不算存活，`mutmut-cicd-stats.json` 里也看不到它——导入期就崩的变异体因此被静默放过。复验命令带 `--continue-on-collection-errors`（收集错误按测试失败计、退出码 1），并且门禁另立红线：`.meta` 里出现既非杀死也非存活的退出码（尤其 2）一律判失败并列出清单，交人工处置。
- **存活变异按三类归因登记，不笼统写「等价变异」**（当前 22 条 = 真等价 6 + 已接受缺口 16 + 真漏测 0）。分档判据只有两条、都可测：**实际输出逐字节相同**（对比时掩掉 `timestamp`），且**把取值喂给库实际使用它的地方后语义相同**（省略的 kwarg 落到与实现相同的默认值、`None` 在库的布尔判定里与 `False` 同效、`fmt` 大小写不敏感）。两条都满足 ⇒ 真等价；输出相同但有效语义确实变成另一个值 ⇒ 已接受缺口（无可观察输出，但配置状态真的不同）。
  1. **真等价 6 条**（全在 `logging.configure_logging`），逐条依据是：
     - `TimeStamper` 省略 `utc=True`：structlog 26.1 的签名默认就是 `utc=True`，删掉这个 kwarg 与显式写它落在**同一分支**——**不是**「默认 `False`、只有环境时区为 UTC 时才成立」；实测连 `get_config()` 里那个 `TimeStamper` 的取值（`("iso", True, "timestamp")`）都完全一致；
     - `fmt="ISO"`：`_make_stamper` 用 `fmt.upper() == "ISO"` 比较，大小写不敏感（字段本身仍是字面 `"ISO"`，但实测输出逐字节相同）；
     - `StreamHandler(stream=None)`：标准库默认就是当时的 `sys.stderr`（实测取值与输出都与显式写法一致）；
     - `ProcessorFormatter` 的 `keep_exc_info` 取 `None` 或删除：签名默认就是 `False`，`None` 与 `False` 在 `if self.keep_exc_info:` 里同效；
     - `JSONRenderer(ensure_ascii=None)`：`json.dumps` 把假值都当「不转义」，与显式 `False` 同效（实测中文事件不被转义、输出逐字节相同）。
     这一组的共同性质是**等价性建立在上游的默认值与判定方式上**：上游改默认值、改 `fmt` 的大小写处理、或改「假值即不转义」的判定，等价就会失真——属**脆弱等价**，所以必须留在册里，不能只靠「删掉 kwarg 也一样」口口相传。`keep_exc_info=True` 那条由 `tests/unit/test_logging_output.py` 的断言杀死。
  2. **已接受缺口 16 条**（所有者裁决：不补）：11 条在 `main.create_app`（FastAPI 的 `title` / `description` / `version` / `lifespan` 元数据，以及 `configure_logging` 的入参透传），5 条在 `logging.configure_logging`——
     - `wrapper_class` 取 `None` 或删除：默认 wrapper 是 `structlog._native.BoundLoggerFilteringAtNotset`（26.1.0 实测：这个类只从 `structlog._native` 暴露，包上取不到 `structlog.BoundLoggerFilteringAtNotset` 这个名字），实现显式传的是 `structlog.stdlib.BoundLogger`，**两者不是同一个类**，只在项目所有可观察输出上一致——**行为等价、配置状态不同**（`get_config()['wrapper_class']` 从前者变成后者）；
     - `cache_logger_on_first_use` 取 `None` / 删除 / `False` 三种写法：实测三者都把 `structlog.get_config()['cache_logger_on_first_use']` 从 `True` 变成 `False`，输出与对照逐字节相同——**真实差异，只是只影响 logger 缓存、无可观察输出**。
     这些改动只能靠**把实现抄进断言**来「发现」，不构成值得测的业务行为。
  3. **真漏测 0 条**：`keep_exc_info=True` 那条被断言杀死，`None` 与删除 kwarg 那两条按第 1 条归入**真等价**——**没有需要补断言的缺口**。
  具体清单以 `scripts/check.sh` 第 5 步当次输出与 `uv run mutmut results` 为准——本册只定口径与归因分类，不复述逐条清单（断言一增补，编号与条数就会变）。
- **变异范围固定为 `tests/unit`**：门禁结论必须与环境无关——容器起没起不能改变门禁是否通过，因此不把需要 Docker 的集成测试纳入变异运行；集成路径由集成测试自身覆盖并由人工核对。
- **`mutate_only_covered_lines = true`**：只变异被测试执行到的行，于是「存活变异」＝**执行过但没有被断言验证**的缺口；「完全没被执行」的行交给第 4 步的 `term-missing` 暴露。两个工具的分工因此不重叠，而不是各说一遍。
- **一条已知盲区：配置行的增删可能完全不在变异分数里**。`logging.configure_logging` 的 console 分支里那行 `format_exc_info` 是**处理器列表里的一个元素**，`mutmut` 不为它生成删除变异——于是把这一行删掉，门禁不会红、覆盖率不掉、**变异分数也不掉**。也就是说：「两种格式渲染口径一致」这条契约**不能靠变异测试维持**。它实际由两条独立防线兜住：`tests/unit/test_logging_output.py` 的断言（去 ANSI 后**恰有一行的行首**是 `Traceback (most recent call last):`——plain traceback 的形态；且输出里不出现 rich 面板框线）与 `docs/testing.md` 的 **R13** 评审项（逐条回到代码确认契约点名的配置行仍在）。两条判据各自的依据与耦合边界写在 `specs/0001-email-forwarding-service/design.md` 的 §4.6 第 2 条。**「变异测试看不见」不等于「没有自动检查」**——这两件事不要混着写。
- **`mutants/` 是工具的工作目录**（源码副本 + 运行缓存），进 `.gitignore`，不提交；门禁每次运行前先删掉它做全量变异（见「实测证据」的取数口径），所以它**不跨次复用**。

**门禁（不跑 CI）**

- **不跑 CI**：`.github/` 下只有 PR 模板与 Issue 模板，**不建 `workflows/`**，也不要新建任何 CI 配置。
- **门禁全部落在本地，没有任何自动兜底**：没有人会在 PR 上替你跑检查，配置错误也不会被流水线拦住。提交前必须自己跑一键脚本并全绿。
- **门禁分两层**：
  1. **pre-commit**——ruff format、ruff lint、commitizen 校验提交信息格式，外加基础文件卫生（行尾空白、文件末尾换行、YAML / TOML 可解析、合并冲突标记、大文件、行尾统一为 LF）。
  2. **一键脚本**——`scripts/check.sh`，固定五步、顺序不可调整：
     `uv run ruff format --check .` → `uv run ruff check .` → `uv run mypy src` → `uv run pytest`（带覆盖率输出）→ `uv run mutmut run`（变异测试**并逐条复验存活变异**；本步固定先清 `mutants/` 做全量变异）。
- 一键脚本的两个刻意设计：**不接受参数**（避免「本地跑的参数与别人不同」导致门禁不可比），**不加「先起容器」逻辑**（让门禁依赖尽量少，才能随手就跑）。任一步失败立即以非零退出码结束，后续步骤不再执行。
- **已知代价**：`git commit --no-verify` 可以跳过 pre-commit，且没有任何远端机制拦截；「这个改动跑过测试了吗」只能靠信任。这是接受不跑 CI 的直接代价，不另做补偿机制。
- **标准不得为了变绿而放宽**：mypy 要求完整类型标注、不允许 `# type: ignore` 兜底；`mypy src` 是门禁的一部分。某个库确实缺类型存根时，处置是先在册里留下结论，再在 `[[tool.mypy.overrides]]` 中按 module 精确放行，**不得整体关闭**。pytest 的标准同样不放宽。
- **本包自己声明 PEP 561 类型化**：`src/vs_email_forwarding/py.typed` 是类型标记（与 `pyproject.toml` 的 `Typing :: Typed` classifier 配套），使「单独对 `tests/` 跑 mypy」不再把本包当无类型第三方库。**门禁范围不变：仍是 `mypy src`（`[tool.mypy] files = ["src"]` 与 `scripts/check.sh` 第 3 步都不动）**；测试不进类型门禁，但手工对 `tests/` 跑 mypy 也要求零问题，作为独立检查（判据与实测见「实测证据」）。
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

- **门禁五项全绿**（在当前工作副本上逐一执行，命令入口即 `scripts/check.sh` 的五个步骤）：
  - `ruff format --check .` → `74 files already formatted`；
  - `ruff check .` → 零告警，退出码 0；
  - `mypy src` → `Success: no issues found in 26 source files`；
  - `pytest --cov --cov-report=term-missing` → `162 passed`，覆盖率 `98%`；
  - `mutmut run` + 存活复验 → **128 个变异体**，mutmut 记 `killed 89 / survived 39`，逐条复验后**真实 `106 杀 / 22 存活`**（变异分数约 83%），退出码 0（未触发任何一条红线）。
- **变异基线的取数口径：门禁自己保证「全量」**。上条数字取自清空 `mutants/` 后的全量重跑，而且第 5 步现在**每次运行都先 `rm -rf mutants/`**（不再靠人的纪律）。原因是实测发现同一份代码能给出不同数字：带残留缓存增量重跑时 `mutmut` 只重新生成它认为变过的文件（它自己的输出是 `done in 891ms (2 files mutated, 0 ignored, 24 unmodified)`），那一轮只跑了 **83 个变异体**、复验后报 `72 杀 / 11 存活`；而**同一份代码**清缓存后是 128 个变异体、复验后 `106 杀 / 22 存活`。同一批工作里还读到过第三种存活数（39，即缓存恰好与工作区同步时）——**11 / 22 / 39 三种取值、代码没变**，根因是 `mutants/` 里那份 `src/` 与 `tests/` 的快照与工作区不一致（复制期间测试正在被改动）。**数字不可复现的基线等于没有基线**，所以清理写进了脚本；代价实测只比增量多约 1 秒（瓶颈在逐条复验，不在变异生成）。手工跑 `uv run mutmut run` 仍是增量，那种数字不要拿去当基线。
- **存活复验步已用正反两面自测**：常态下它把 17 条 `db/models` 变异体（mutmut 记「存活」）改判为杀死；反证做过两次、互相独立：① 临时把 `tests/unit/test_repository_statements.py` 里一条入站插入语句的断言弱化，复验报出的存活清单随即多出 `inbound.x_inbound_pending_insert_statement` 那组变异体（实测多 5 条）；② 评审侧在冻结沙箱里去掉 `db/models` 的 CHECK 断言，真实存活数上升、其中 4 条 `db.models` 变异体重新进入真实存活清单。两次反证都同时验证了两端：断言确实在断言，复验步确实在量真实行为（临时改动均已逐字节还原）。
- **存活复验与容器状态无关**：变异与复验都固定只选 `tests/unit` 全量（`tests/unit` 里没有任何 `integration` 标记的用例；本册不复述会随断言增补漂移的条数，当次条数以 `uv run pytest --collect-only tests/unit` 为准），实测把 `DATABASE_URL` 指向一个关闭的端口后再复验，判定完全不变（真存活的仍退出码 0、该被杀的仍退出码 1）。因此「容器起没起」不影响第 5 步的结论。
- **覆盖率与变异分数的分工**（同一次快照上覆盖率 98%、复验后变异分数约 83% = 106/128）——这正是把覆盖率降为诊断指标、把断言质量交给变异测试的实测依据：覆盖率说明「大部分行被执行过」，变异分数说明「哪一部分行为没有被断言验证」。复验后仍存活的 22 条按「测试质量的度量口径」那节的三类归因处理，集中在 `logging.py` 与 `main.py`（阶段 1 存量模块）——这些代码**被执行过**（覆盖率不为零），但没有断言验证过那些行为；`db/models.py` 上那 17 条则根本不是缺口，是 mutmut 的度量缺陷。覆盖率看不出这两种情况的区别，变异测试（复验后）看得出。
- **第 5 步的红线已逐条自测**：三条原有红线用合成统计验证——`killed=0 / survived=5` → 退出码 1（全部存活＝同义反复）；`total=0` → 退出码 1（配置失效）；`killed=3 / survived=2` → 退出码 0（正常放行）。复验相关的三条也实测过：不设变异体时全套 `tests/unit` 通过、`MUTANT_UNDER_TEST=fail` 退出码非 0（两条对照）；把某个 `.meta` 的退出码人工改成 2 → 门禁退出码 1 并打印中文原因（「mutmut 把 2 当成 check was interrupted by user，等于静默放过」）。
- **第 5 步的运行成本可接受，所以可以每次都全量**：整步实测约 30 秒（`mutmut run` 全量重跑约 3 秒 + 逐条复验 39~40 条约 24 秒；单条复验跑 `tests/unit` 全量约 0.5 秒），而增量与全量实测只差约 1 秒——瓶颈在逐条复验，不在变异生成。这正是「门禁脚本不吃参数」与「变异范围固定」两条设计能成立的前提，也是「把全量重跑写进脚本」这条决定能成立的前提。
- **新增开发依赖的许可**：`mutmut` BSD-3-Clause、`pytest-cov` MIT、`coverage` Apache-2.0、`libcst` MIT、`textual` MIT、`setproctitle` BSD-3-Clause——全部为宽松许可，与本仓库的 MIT 兼容，未引入 copyleft。
- **`ruff check` 的全部告警只来自 `RUF001` / `RUF002` / `RUF003`，零处来自其他规则**。把这三条从 `ignore` 中移出后复现（`ruff check . --select RUF001,RUF002,RUF003 --statistics`）：共 **314 处**，其中 `RUF002` 191 处、`RUF001` 92 处、`RUF003` 31 处，**没有第四种规则出现**。当前 `ignore` 生效时同一份代码报 0 处，且没有冒出任何其他规则的告警——这正是「只有中文标点这一处例外，其余标准未动」的直接证据。
- 同一次 `pytest` 还输出一条 `StarletteDeprecationWarning`：`Using httpx with starlette.testclient is deprecated; install httpx2 instead`，来源是虚拟环境中的 `fastapi/testclient.py`。它说明**测试是跑通的**（`httpx` 在 3.13 上实际可用），风险性质是「将来会不会被上游弃用」，已登记为未定项 E2。
- **`uvloop` 在 Windows 上确实不装**：`uv.lock` 中该依赖带 `sys_platform != 'win32'` 标记；开发机（Linux）实测已装 `uvloop 0.22.1`，生产同为 Linux，同样可获得。
- **解释器与依赖一律交给 uv 管理**：虚拟环境中的解释器实测为 **Python 3.13.16**，与 `.python-version` 的 `3.13` 一致；「一律用 `uv run`」是为了让解释器与依赖只由一处决定，而不是因为存在版本不一致。
- **`.github/` 下没有 `workflows/`**：实测只有 `ISSUE_TEMPLATE/`（`bug_report.md`、`feature_request.md`）与 `PULL_REQUEST_TEMPLATE.md`。
- **工具链与依赖的实测版本**（取自当前虚拟环境）：ruff 0.16.9、mypy 2.3.1、pytest 9.1.1、commitizen 4.19.0、pre-commit 4.6.2；运行时 fastapi 0.141.1、uvicorn 0.54.0、pydantic-settings 2.15.0、structlog 26.1.0、`sqlalchemy` 2.1.4、`greenlet` 3.5.6（`sqlalchemy[asyncio]` 的 extra 依赖，见本节「`sqlalchemy[asyncio]` 的实测证据」条）、`alembic` 1.20.0、`asyncpg` 0.32.0、`IMAPClient` 4.1.0；测试依赖 httpx 0.28.1、starlette 1.7.0。
- **门禁全绿的适用范围**：业务逻辑尚未实现，`src/` 下只有包占位与一个能返回 `/healthz` 的最小入口。因此上述绿只证明**工程配置与工具链可用**，不证明任何业务正确性。
- **依赖许可与类型标注的实测结果**见 `specs/0001-email-forwarding-service/design.md` 的「实测证据」一节（本册不复述）。
- **两个无类型存根库的实测结论**：`asyncpg` 与 `imapclient` 的包目录下都不存在 `py.typed`，在 `src/` 下 import 它们后跑 `mypy src` 各报一条 `Skipping analyzing "…": module is installed, but missing library stubs or py.typed marker [import-untyped]`；`sqlalchemy` 与 `alembic` 都有 `py.typed`，因此不放行——这正是放行范围只覆盖前两个的依据。**这两条放行还没有落进 `pyproject.toml`**（该文件 mypy 段记有这条处置）：没有对应 import 的 `[[tool.mypy.overrides]]` 会被 mypy 报成 `unused section(s)`，所以放行与第一个真正 import 该库的模块同一次改动落地。
- **本包 `py.typed` 的实测结论**（阶段 2 收口后补）：`src/vs_email_forwarding/` 此前没有 `py.typed`（而 `pyproject.toml` 已带 `Typing :: Typed` classifier，属声明与实物不一致）。补 marker 前单独对测试树跑 mypy 会把本包当无类型第三方库：`uv run mypy tests` → **17 条**（16 条 `import-untyped` + 1 条 `var-annotated`）；补 marker 后同命令 **Success（6 files）**。marker 让类型真正流进测试代码，随即暴露两条此前被 `Any` 掩盖的真实问题并已修掉（`test_repositories.py` 的 `FromClause` 没有 `.delete()`；`test_repository_statements.py` 向 `_iter_clause_elements` 传了 `ColumnElement | None`），修后 `uv run mypy src tests` → **Success（32 files）**。构建产物实测：`uv build` 的 wheel 含 `vs_email_forwarding/py.typed`（`RECORD` 有条目），sdist 含同路径，**无需 `[tool.hatch.build]` 配置**（hatchling 默认收录包目录下全部文件）；当前门禁的五个数字见本节第 1 条。
- **`sqlalchemy[asyncio]` 的实测证据**（随第一个异步引擎模块落地）：启用 extra 前虚拟环境里没有任何 `greenlet`（`grep -c 'name = "greenlet"' uv.lock` → **0**；`import greenlet` → `ModuleNotFoundError: No module named 'greenlet'`），`import sqlalchemy.ext.asyncio` 实测失败：`ImportError: The SQLAlchemy asyncio module requires that the Python 'greenlet' library is installed.  In order to ensure this dependency is available, use the 'sqlalchemy[asyncio]' install target:  'pip install sqlalchemy[asyncio]'`，该异常的 `__cause__` 为 `ModuleNotFoundError: No module named 'greenlet'`。`sqlalchemy-2.1.4.dist-info/METADATA` 实测含 `Provides-Extra: asyncio` 与 `Requires-Dist: greenlet>=1; extra == "asyncio"`——即 `greenlet` 由 SQLAlchemy 自身声明、**不引入新上游**；同一份 METADATA 里 `[postgresql-asyncpg]`、`[aiomysql]`、`[aioodbc]`、`[asyncmy]`、`[aiosqlite]` 五个 extra 也都以 `sqlalchemy[asyncio]` 为前置。启用 extra 后 `uv sync` 只新增 `greenlet==3.5.6`（`Requires-Python: >=3.10`，本机 Python 3.13 装到 cp313 wheel），`uv.lock` 在 sqlalchemy 包的 `[package.optional-dependencies]` 下记录 `asyncio = [{ name = "greenlet" }]`——它确实来自该 extra，而不是新增的顶层依赖。许可实测：`greenlet-3.5.6.dist-info/METADATA` 的 `License-Expression: MIT AND PSF-2.0`，随包 `License-File: LICENSE` 与 `LICENSE.PSF`；两段都是宽松许可，与本仓库 MIT 兼容，未引入 copyleft。启用后再测 `import sqlalchemy.ext.asyncio` 正常导入（sqlalchemy 2.1.4）。

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
- **把变异分数设成百分比阈值**（例如核心域 ≥80%、一般逻辑 ≥60–70%）—— 原因：数字一旦成为目标就会被反向优化（写同义反复测试、滥用 `# pragma: no mutate` 把存活抹掉），而且没有实测依据支撑任何具体取值；当前基线是 128 个变异体里**复验后** 106 个被杀（约 83%），阈值一起步就会变成「补断言」或「降门槛」的二选一。门禁只卡结构性红线，存活清单交人工评审。
- **只加覆盖率、不加变异测试** —— 原因：覆盖率衡量执行、不衡量验证。当前工作副本上 128 个变异体里复验后仍有 22 个存活，而覆盖率是 98%——这一类「执行了但没被断言验证」的缺口只有变异测试看得见。
- **直接采信 `mutmut results` 的存活清单**（拿它当「哪些行为没被验证」的结论）—— 原因：见「测试质量的度量口径」那两条坑——mutmut 只给每个变异体挑 1~2 条用例，且把退出码 2 当「用户中断」，清单里混着 17 条其实会被杀死的 `db/models` 变异体；存活必须经**全套 `tests/unit` 复验**才可采信。
- **把 `forkserver_warmup` 改成 `none` 来修存活误报** —— 原因：误报的根因在 **mutmut 的用例选择**（每个变异体只挑 1~2 条相关用例，导入期构造的 `CHECK` 约束被归因给无关用例），不在预热方式；实测把 `forkserver_warmup` 改成 `none` 后逐条判定结果不变，`db/models` 那批仍被记成存活。
- **把 pytest 退出码 2 当正常值放过**（沿用 `mutmut/stats.py` 的 `check was interrupted by user` 映射）—— 原因：退出码 2 是收集/导入错误，导入期就崩的变异体因此既不进 killed 也不进 survived，`mutmut-cicd-stats.json` 里根本看不到它；复验命令带 `--continue-on-collection-errors` 让它按失败计（退出码 1），并且 `.meta` 里再出现 2 一律判门禁失败。
- **保留 `mutants/` 缓存、让门禁做增量重跑**（把基线数字建立在增量结果上，或只在文档里叮嘱「比较前先清缓存」）—— 原因：增量重跑时 `mutmut` 只重新生成它认为变过的文件，实测同一份代码曾给出 11 / 22 / 39 三种存活数而门禁全绿；一个会随残留状态变数字的门禁违背它自己的设计意图（「刻意不接受参数，保证所有人跑的是同一套门禁」）。清理已写进 `scripts/check.sh` 第 5 步，全量重跑只比增量多约 1 秒。
- **把集成测试纳入变异运行范围** —— 原因：门禁结论会随容器是否启动而改变，直接破坏「所有人跑的是同一套门禁」这条前提。
- **不在门禁里跑变异、只留一条文档命令** —— 原因：本项目不跑 CI，没有任何自动兜底；不进门禁就等于不存在，只会退化成口头约定。
- 使用 mutmut 2 —— 原因：mutmut 3+ 的执行模型支持函数级缓存与增量重跑（实测重跑约 3 秒），2 的模型做不到，会让门禁慢到没人愿意跑。
- 把放行写成**没有对应 import 的** `[[tool.mypy.overrides]]` —— 原因：mypy 会把它报成 `unused section(s)` 并打进 `scripts/check.sh` 的输出，制造一条需要被忽略的噪音，也等于留一条无代码支撑的例外；放行只随真正 import 该库的模块落地。
- **手工把 `greenlet` 写成 `pyproject.toml` 的顶层依赖**（而不是用 `sqlalchemy[asyncio]`）—— 原因：`greenlet>=1` 是 SQLAlchemy 为 `asyncio` extra 声明的兼容性前提，手工再钉一份等于把上游约束复制到本仓库，上游一改就漂移；走 extra 让「谁需要它、需要哪个版本」只留在 SQLAlchemy 的元数据一处。同理**不把 `greenlet` 登记成第五项「数据访问依赖」**——它不是本项目选中的能力，只是异步驱动的实现细节。

## 落地位置

- `pyproject.toml` —— 运行时依赖清单（四项基础依赖 + 四项数据与邮件访问依赖 `sqlalchemy[asyncio]` / `alembic` / `asyncpg` / `IMAPClient`）与 `[dependency-groups] dev` 分组、`requires-python`、`[project] version`，以及 ruff / mypy / pytest / commitizen 的全部配置；`RUF001` / `RUF002` / `RUF003` 的 `ignore` 与理由注释就在此处。**针对无类型库的 `[[tool.mypy.overrides]]` 按 module 精确放行**（连同理由，只覆盖实测报 `import-untyped` 的 `asyncpg` 与 `imapclient`，随第一个 import 它们的模块一起加）也落在本文件；**这些依赖的选型理由与代价**见 `specs/0001-email-forwarding-service/design.md`。
- `.python-version` —— 锁定 3.13（不得改动）。
- `uv.lock` —— 依赖解析与锁定的唯一真源，随依赖变更一起提交。
- `.pre-commit-config.yaml` —— pre-commit 挂钩清单与 `commit-msg` 阶段；启用命令为 `uv run pre-commit install --install-hooks` 与 `uv run pre-commit install --hook-type commit-msg`。
- `scripts/check.sh` —— 一键门禁的唯一入口（必须 LF、无 BOM）；Windows 版 `check.ps1` 已删除。第 5 步的**存活复验**（逐条用全套 `tests/unit` 重跑存活变异体、按复验退出码重新认定）与**固定先清 `mutants/` 做全量变异**（保证数字可复现）都落在这里。
- `scripts/README.md` —— 脚本清单、门禁为什么重要、与 Docker 的关系、五步门禁各自的判据与常见问题处置。
- `tests/unit/` —— 不依赖数据库与网络的纯逻辑单测，边界写在 `tests/unit/README.md`；需要真实 PostgreSQL 与邮件服务的用例放 `tests/integration/`。
- `docs/open-questions.md` —— 未定项的唯一清单（本册「未定项」段只放指针）。
- `docs/testing.md` —— **测试规范与测试说明的唯一出处**：单测 / 集成测试规则、AI 写测试的作业流程、评审检查表、pytest 标记约定。门禁的「怎么跑」在 `scripts/README.md`，「什么算合格」在本文件与 `docs/testing.md`。
- `pyproject.toml` 的 `[tool.mutmut]` 与 `[tool.coverage.*]` —— 变异与覆盖率的**配置**（变异范围、只变异被覆盖的行、覆盖率 source 与排除项）。**门禁的判据不写在这里**（配置能改，判据必须只有一处），判据在 `scripts/check.sh`。
- `.gitignore` —— `mutants/`（变异测试的工作目录：源码副本与运行缓存）；覆盖率产物 `.coverage` / `coverage.xml` / `htmlcov/` 此前已在忽略清单里。
- `deploy/` —— 构建与交付脚本的落点；`DOCKER_BUILDKIT=0` 的显式拦截必须落在其中的构建脚本里（脚本尚未实现，见未定项 D2）。
- `AGENTS.md` —— §3 命令入口、§7 编码约定、§9 提交与 PR、§10 不要做的事，与本册互为规则与结论的分工。

## 未定项

以下问题**尚未决定**，详见 `docs/open-questions.md`；实现时**不得替它们挑一个「合理的默认值」**：

- E2 `httpx` 与 `httpx2` 的迁移时机（当前测试会打 StarletteDeprecationWarning，提示改用 httpx2）
- D1 各基础镜像 tag 的 digest 需在写进脚本前复核
- D2 部署脚本的具体形态（尚未实现）
