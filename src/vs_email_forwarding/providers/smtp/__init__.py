#!/usr/bin/env python3
"""SMTP 发信适配。

发信走 SMTP 直连（企业邮箱 / 自建 MTA），主机、端口、账号、口令、是否 STARTTLS
全部来自 ``.env``，由 :func:`vs_email_forwarding.config.get_settings` 统一读取。

不放：业务状态流转、重试状态机（重试策略属于 ``vs_email_forwarding.core`` 与 ``flows``）。

客户端库选型已定：Python 标准库 ``smtplib``（经 ``asyncio.to_thread`` 调用，不新增运行时
依赖，``design.md`` §5）。接口协议见本目录 ``base.py``，实现在 ``smtplib_sender.py``。
"""
