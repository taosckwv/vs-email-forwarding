# 集成测试

放什么：需要真实外部依赖才能验证的用例，重点是流程与一致性，而不是纯逻辑断言。

## 依赖怎么起

集成测试需要**真实 PostgreSQL** 与 **Mailpit**（本地 SMTP/IMAP 回路验证）：

```powershell
./scripts/dev-up.ps1                              # Windows
docker compose -f compose.dev.yaml up -d          # 通用 / Linux
```

## 数据库隔离

测试库为 `mail_relay_test`，与开发库 `mail_relay_dev` **分离**：集成测试允许清表，
但绝不允许碰开发库。连接串通过环境变量 / `.env` 覆盖为测试库，不要改仓库里的配置文件。

## 覆盖重点

1. **状态机转换**：待审 → 已排队 → 已发送 → 已收到 / 拒审 / 终态失败，以及非法转换必须被拒绝。
2. **单次 tick 的幂等性**：同一条记录被同一个流重复处理时结果不变，不产生重复副作用。
3. **并发抢占不重复发信**：四条流并发运行时，一条待发记录只能被一个 worker 抢到，
   依靠 `SELECT ... FOR UPDATE SKIP LOCKED` 与状态字段，禁止「先 SELECT 判断、再 UPDATE」。
4. **回复关联**：用 `References` / `In-Reply-To` 关联回原邮件；关联不上的标记为孤儿但**仍然入库，不丢数据**。

不放什么：纯逻辑断言（放 `tests/unit/`）。

约定：本目录不放 `__init__.py`；文件名 `test_*.py`。测试数据（邮件样本等）放
`tests/fixtures/`。
