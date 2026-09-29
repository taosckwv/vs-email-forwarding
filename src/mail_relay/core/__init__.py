"""跨层纯逻辑。

职责：不涉及 I/O 的纯函数与值对象，例如：
  * Message-ID 生成；
  * 状态机（pending / queued / sent / received / rejected / failed 等转换规则）；
  * 重试与退避策略（最多 3 次，退避 15s / 30s / 60s）。

不放：I/O 与数据库访问。
"""
