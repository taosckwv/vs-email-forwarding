#!/usr/bin/env python3
"""IMAP 收信适配。

收信走 IMAP：单专用回复邮箱 + UID 游标增量拉取 + 用 ``References`` / ``In-Reply-To``
关联原邮件。**只有关联上的用户回复与关联上的退信入库；其余一律丢弃、不入库**，
丢弃只在日志记条数。退信按结构识别（``multipart/report`` + ``message/delivery-status``），
不靠主题或正文猜；阅读回执（MDN）识别后即丢弃，不得当作回复。

不放：业务状态流转、UID 游标持久化（游标落库属于 ``vs_email_forwarding.db`` 仓储层）。

客户端库选型已定：``IMAPClient``（同步库，经 ``asyncio.to_thread`` 调用，``design.md`` §5）。
接口协议见本目录 ``base.py``，实现在 ``imapclient_fetcher.py``。
"""
