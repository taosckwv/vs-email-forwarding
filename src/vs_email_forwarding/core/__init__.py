#!/usr/bin/env python3
"""跨层纯逻辑。

职责：不涉及 I/O 的纯函数与值对象，例如：
  * Message-ID 生成（``message_id``）；
  * 六态状态机判定（``states``）；六态统一取值的真源是 ``design.md`` §2.3 与
    ``vs_email_forwarding.constants.MESSAGE_STATES``（库内与对外同一套、无映射层，
    本层不另立词表）；
  * 重试与退避策略（``retry``；最多 3 次重试，退避 15s / 30s / 60s）；
  * 入站邮件纯判定：DSN / MDN 结构与 ``In-Reply-To`` / ``References`` 关联（``inbound``）。

不放：I/O 与数据库访问。
"""
