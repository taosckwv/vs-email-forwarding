# 变更日志

本文件记录本项目所有值得注意的变更。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

> **维护方式：手工维护。**
> 本项目**不使用** python-semantic-release 之类的自动发版工具；commitizen 只负责提交信息校验与辅助发版。
> 版本号与 `pyproject.toml` 中的 `version` 由维护者手工同步，发版时把 `Unreleased` 段整体整理为对应的版本段即可。

## [Unreleased]

### 新增

- 内部邮件中转服务第一期：调用方经 HTTP API 提交邮件，由服务负责安全审查、SMTP 投递、回复回收与保留期清理。
  - HTTP API 三端点：`POST /v1/messages` 提交、`GET /v1/messages/{id}` 查状态、`GET /v1/messages/{id}/replies` 查回复（keyset 游标分页）；`/healthz` 补一次轻量数据库探测。
  - 后台流：四条业务流（审查出站、发送、拉取入站、审查回复）外加保留期清理流，随应用 lifespan 启停；认领（`FOR UPDATE SKIP LOCKED`）→ 调用 → 落库三阶段幂等推进，重试上限与退避表按既定硬约束，第 4 次尝试的崩溃遗留行由收割支推进终态。
  - 数据模型与首版迁移：出站 / 入站共用的邮件表、待审表、拒审表、IMAP 游标表四张表，经 Alembic 首版迁移一次建齐（含索引与 `CHECK`）。
  - 真实依赖集成测试族：每次运行自建运行级隔离 PostgreSQL 测试库并跑真实迁移，配进程内假 SMTP（捕获线上字节）、假 IMAP 与可替换假审查；数据库语义不用 mock。
  - 本地门禁配套：启动即校验三判据、跨层静态检查（接口契约一致、三方取值一致、抢占写法唯一、配置只经配置层读）、`docs/api-contract.yaml` 作为对外契约唯一真源。

### 变更

（暂无）

### 修复

（暂无）
