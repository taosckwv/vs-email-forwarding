# 集成测试

放什么：需要真实外部依赖才能验证的用例，重点是流程与一致性，而不是纯逻辑断言。

## 依赖怎么起

集成测试需要**真实 PostgreSQL** 与 **Mailpit**（本地 SMTP/IMAP 回路验证）：

```powershell
./scripts/dev-up.ps1                              # Windows
docker compose -f compose.dev.yaml up -d          # 通用 / Linux
```

## 数据库隔离

测试库为 `vs_email_forwarding_test`，与开发库 `vs_email_forwarding_dev` **分离**：集成测试允许清表，
但绝不允许碰开发库。连接串通过环境变量 / `.env` 覆盖为测试库，不要改仓库里的配置文件。

## 覆盖重点

1. **状态机转换**：待审 → 已排队 → 已发送 → 已收到 / 拒审 / 终态失败，以及非法转换必须被拒绝。
2. **单次 tick 的幂等性**：同一条记录被同一个流重复处理时结果不变，不产生重复副作用。
3. **并发抢占不重复发信**：四条流并发运行时，一条待发记录只能被一个 worker 抢到，
   依靠 `SELECT ... FOR UPDATE SKIP LOCKED` 与状态字段，禁止「先 SELECT 判断、再 UPDATE」。
4. **入站分拣与回复关联**：用 `References` / `In-Reply-To` 关联回原邮件；退信按
   `multipart/report` + `message/delivery-status` 结构识别并置原邮件为发送失败；
   阅读回执（MDN）必须被剔除而不得当成回复；**关联不上的入站邮件必须不入库**，只留丢弃条数日志。

不放什么：纯逻辑断言（放 `tests/unit/`）。

约定：本目录不放 `__init__.py`；文件名 `test_*.py`。测试数据（邮件样本等）放
`tests/fixtures/`。
