#!/usr/bin/env python3
"""用例层：同步请求路径的业务编排。

职责：面向 HTTP 请求的业务用例——提交邮件、查询邮件状态、查询邮件回复、数据库可用性探测；
事务边界、调用哪个仓储、按需调用哪个适配器都由本层决定。

不放：HTTP 请求校验与响应序列化（走 ``vs_email_forwarding.api``）、SQL（走
``vs_email_forwarding.db`` 仓储层）、外部能力的直接调用（走
``vs_email_forwarding.providers``）、定时任务（走 ``vs_email_forwarding.flows``）。

分层与依赖方向见 ``docs/architecture.md``。
当前状态：同步请求路径的四个用例都已落地——``submit_message.py``（提交：生成 ``Message-ID`` →
写出站待审行 → 返回 ``{id, status}``）、``query_message.py``（状态查询：``REPEATABLE READ``
单事务快照内判定六态 → 转成 ``MessageStatusResponse``）、``query_replies.py``（回复查询：
``received_at ASC, id ASC`` 的 keyset 翻页 → 转成 ``RepliesPageResponse``）、``health.py``
（健康探测：开一次短会话做一次探测查询，失败原样向上抛，由 ``api/`` 转 ``503``）。
用例编排与字段定义见 ``specs/0001-email-forwarding-service/design.md`` §3.1 / §4.1 / §8 与
``plan.md`` §2.5。
"""
