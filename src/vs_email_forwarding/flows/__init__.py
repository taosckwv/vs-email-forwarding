"""四条轮询流与调度器。

四条流（默认间隔均为 10 秒，间隔只能从配置读取，不得写死）：
  1. ``review_outbound``：审查待审表中的出站邮件，通过则写入邮件表，不通过则入拒审表；
  2. ``send_email``：查出未发送记录并通过 SMTP 投递，失败按 15s / 30s / 60s 退避重试 3 次；
  3. ``fetch_inbound``：通过 IMAP 按 UID 游标增量拉取专用回复邮箱的新回复；
  4. ``review_inbound``：对拉取到的入站回复做同样的安全审查。

约束：
  * 四条流是**进程内 asyncio 任务**，彼此独立、并发运行，随 FastAPI lifespan 启停；
  * **每个流的单次 tick 必须幂等**——多流并发下抢占任务只能用
    ``SELECT ... FOR UPDATE SKIP LOCKED`` 配合状态字段与 ``next_attempt_at``，
    禁止「先 SELECT 判断、再 UPDATE」的两步写法。

不放：HTTP 路由、SQL 字符串。

当前状态：仅占位，尚未实现；调度规格见 specs 中的调度规格文档。
"""
