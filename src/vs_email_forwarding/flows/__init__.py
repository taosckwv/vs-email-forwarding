#!/usr/bin/env python3
"""定时任务层：五条后台任务的入口与编排。

五条后台任务（四条业务流的默认间隔均为 10 秒，间隔与每批条数只能从配置读取、不得写死；
保留期清理的间隔是独立配置项 ``POLL_CLEANUP_SECONDS``，默认 3600 秒）：
  1. ``review_outbound``：审查待审表中的出站邮件，通过则写入邮件表，不通过则入拒审表；
  2. ``send_email``：查出未发送记录并通过 SMTP 投递，失败按 15s / 30s / 60s 退避重试 3 次；
  3. ``fetch_inbound``：通过 IMAP 按 UID 游标增量拉取专用回复邮箱的新回复；
  4. ``review_inbound``：对拉取到的入站回复做同样的安全审查；
  5. 保留期清理：删除到期的邮件表与拒审表记录（回复由外键级联删除）；待审表是纯队列，不参与清理。

约束：
  * 五条任务都是**进程内 asyncio 任务**，各自独立循环、并发运行，随 FastAPI lifespan 启停；
  * **每条任务的单次 tick 必须幂等**——多任务并发下抢占只能用
    ``SELECT ... FOR UPDATE SKIP LOCKED`` 配合状态字段与 ``next_attempt_at``，
    禁止「先 SELECT 判断、再 UPDATE」的两步写法。

本层只做定时任务的入口与编排；同步请求路径的业务编排在 ``vs_email_forwarding.service``。
不放：HTTP 路由、SQL 字符串。

分层与依赖方向见 ``docs/architecture.md``。
当前状态：仅占位，尚未实现；调度规格见 specs 中的调度规格文档。
"""
