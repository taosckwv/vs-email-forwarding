"""用例层：同步请求路径的业务编排。

职责：面向 HTTP 请求的业务用例——提交邮件、查询邮件状态、查询邮件回复；
事务边界、调用哪个仓储、按需调用哪个适配器都由本层决定。

不放：HTTP 请求校验与响应序列化（走 ``vs_email_forwarding.api``）、SQL（走
``vs_email_forwarding.db`` 仓储层）、外部能力的直接调用（走
``vs_email_forwarding.providers``）、定时任务（走 ``vs_email_forwarding.flows``）。

分层与依赖方向见 ``docs/architecture.md``。
当前状态：仅占位，尚未实现。
"""
