#!/usr/bin/env python3
"""HTTP 接口层。

职责：请求解析与校验、响应序列化、依赖注入、状态码与错误映射。
业务编排不在本层——同步请求的用例走 ``vs_email_forwarding.service``。
不放：SQL（走 ``vs_email_forwarding.db`` 仓储层）、业务规则、SMTP/IMAP 的直接调用
（走 ``vs_email_forwarding.providers``）、定时任务（走 ``vs_email_forwarding.flows``）。

分层与依赖方向见 ``docs/architecture.md``。
"""
