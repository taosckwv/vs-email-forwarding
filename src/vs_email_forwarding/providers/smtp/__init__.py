"""SMTP 发信适配。

发信走 SMTP 直连（企业邮箱 / 自建 MTA），主机、端口、账号、口令、是否 STARTTLS
全部来自 ``.env``，由 :func:`vs_email_forwarding.config.get_settings` 统一读取。

不放：业务状态流转、重试状态机（重试策略属于 ``vs_email_forwarding.core`` 与 ``flows``）。

当前状态：仅占位。具体客户端库选型与接口协议（``base.py``）尚未定案。
"""
