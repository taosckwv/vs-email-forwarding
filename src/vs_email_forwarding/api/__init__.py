"""HTTP 路由层。

职责：请求解析与响应编排、依赖注入、状态码与错误映射。
不放：SQL（走 ``vs_email_forwarding.db`` 仓储层）、业务规则、SMTP/IMAP 的直接调用
（走 ``vs_email_forwarding.providers``）。
"""
