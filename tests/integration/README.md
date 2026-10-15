# 集成测试

放什么：需要真实外部依赖才能验证的用例，重点是流程与一致性，而不是纯逻辑断言。

## 依赖怎么起

唯一前提是**真实 PostgreSQL**（`bash scripts/dev-up.sh` 起）。SMTP / IMAP / 审查在测试里用
进程内替身（落点见下节「夹具」）——**进程内替身为主，GreenMail 只作「真回路」的兜底验证**
（`specs/0001-email-forwarding-service/plan.md` §7.2）：

> GreenMail **按账号分信箱**：投给 `reply@example.com` 的信要用该账号登录 IMAP 才看得到，
> 这正好贴合 `IMAP_USERNAME` 的用法。它启动时自带 `auth.disabled`，账号口令随便填。
> 消息只活在容器生命周期里（没挂卷），`down` 之后信箱自然清空。

```bash
bash scripts/dev-up.sh                            # 开发机（Linux）
docker compose -f compose.dev.yaml up -d          # 等价写法
```

## 夹具

共同夹具在 `tests/integration/conftest.py`：

- **真实 PostgreSQL 运行级隔离库**：每次 pytest 运行自建测试库，建库后跑一次
  `alembic -c migrations/alembic.ini upgrade head` 建表，运行结束 `DROP DATABASE ... WITH (FORCE)` 删除；
  每个用例前后对隔离库做真实 `TRUNCATE` 清表（表名取自 ORM 元数据）。
- **三条进程内替身**：假 SMTP 是进程内的真实 SMTP 协议服务器（投递仍走生产适配器与标准库
  `smtplib`，`From` / `Reply-To` 在线上字节上被验证）；假 IMAP 与假审查是 `providers/` 接口的
  进程内实现，替换点在 `flows/fetch_inbound.py` 的装配函数与 `providers/review/factory.py` 的工厂。
- **依赖不可达时整组 skip**：收集钩子探测 PostgreSQL 可达性，不可达就把本目录用例全部标记
  skip，并打印是哪一项依赖不可达——**跳过不等于通过**。

## 数据库隔离

测试库**不用手工建**：夹具每次运行自建形如 `vs_email_forwarding_test_<pid>_<随机 8 位>` 的
隔离库，开发库从不被连接——连接串（环境变量 `VS_EMAIL_FORWARDING_TEST_DATABASE_URL` 显式
指定，否则取配置层 `DATABASE_URL`）只当**服务器坐标**用，库名一律由夹具自己生成，仓库里的
配置文件一个字不改。清表与删库只发生在自建的隔离库上。

## 覆盖重点

1. **状态机转换**：出站邮件沿六态状态机单调推进（六态统一取值见
   `vs_email_forwarding.constants.MESSAGE_STATES` 与 `specs/0001-email-forwarding-service/design.md`
   §2.3，库内与对外同一套、无映射层），非法转换必须被拒绝（带谓词的条件 UPDATE）。
2. **单次 tick 的幂等性**：同一条记录被同一个流重复处理时结果不变，不产生重复副作用。
3. **并发抢占不重复发信**：四条流并发运行时，一条待发记录只能被一个 worker 抢到，
   依靠 `SELECT ... FOR UPDATE SKIP LOCKED` 与状态字段，禁止「先 SELECT 判断、再 UPDATE」。
4. **入站分拣与回复关联**：用 `References` / `In-Reply-To` 关联回原邮件；退信按
   `multipart/report` + `message/delivery-status` 结构识别并置原邮件为发送失败；
   阅读回执（MDN）必须被剔除而不得当成回复；**关联不上的入站邮件必须不入库**，只留丢弃条数日志。

不放什么：纯逻辑断言（放 `tests/unit/`）。

约定：本目录不放 `__init__.py`；文件名 `test_*.py`。测试数据（邮件样本等）放
`tests/fixtures/`。

规则与标记：**该写什么、断言落点、评审检查表见 `docs/testing.md`**（唯一出处，本文件不复述）；
本目录的用例必须打 `@pytest.mark.integration`，容器未起时按标记跳过——**跳过不等于通过**，
涉及数据库语义的验收必须先在起了容器的前提下跑完（`docs/testing.md` §6、§8）。
