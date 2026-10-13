#!/usr/bin/env python3
"""出站 `Message-ID` 的生成与发件地址的取域解析（纯逻辑）。

`core/` 按分层约定不做任何 I/O（`docs/architecture.md`）：本模块不读配置、
不碰数据库与网络——行主键 ``id`` 与发件地址一律由调用方作为参数传入
（`specs/0001-email-forwarding-service/design.md` §3.1）。取域函数同时是启动
校验「`MAIL_FROM_ADDRESS` 能被解析出邮件域」这条判据的落点（同文档 §4.4
判据 2），校验的编排本身在 `config.py`，不在本模块。
"""

from __future__ import annotations

import re
import uuid

__all__ = ["build_message_id", "extract_domain"]

#: 尖括号地址段 ``<...>``：显示名之外真正的地址载体（RFC 5322 的 angle-addr）。
_ANGLE_ADDR_PATTERN: re.Pattern[str] = re.compile(r"<([^<>]*)>")


def extract_domain(address: str) -> str | None:
    """从发件地址解析邮件域；取不出时返回 ``None``。

    规则（`design.md` §3.1）：取 ``@`` 之后的内容；地址带显示名时，先取**最后
    一个** ``<...>`` 内的地址再取域。返回 ``None`` 表示该地址解析不出邮件域
    ——启动校验据此拒绝残缺配置（`design.md` §4.4 判据 2），因此这里不做
    「猜测式」兜底（例如拿显示名里的 ``@`` 后段充当域名）。
    """
    if not address:
        return None
    angle_parts = _ANGLE_ADDR_PATTERN.findall(address)
    candidate = (angle_parts[-1] if angle_parts else address).strip()
    if "@" not in candidate:
        return None
    # 域内不会出现 @：按最后一个 @ 切，避开显示名 / 本地部分混入的分隔符。
    domain = candidate.rsplit("@", 1)[1].strip()
    return domain or None


def build_message_id(row_id: uuid.UUID, mail_from_address: str) -> str:
    """生成出站邮件的 ``Message-ID``，形如 ``<{UUID}@{邮件域}>``。

    UUID 部分即该行主键 ``id``（参数传入），域名部分取自发件地址——与
    `design.md` §3.1 的生成规则一致。该值自提交时刻起跨表原样搬运，是入站
    回复关联的唯一依据，因此不掺入时间戳或随机数。
    取不出邮件域时抛 :class:`ValueError`：正常流程下启动校验（§4.4）已拦住
    残缺配置，走到这里说明不变量被破坏，宁可直接失败也不拼出 ``<uuid@>``
    这类残缺值。异常信息只描述事实、不回显地址（`AGENTS.md` §8.4）。
    """
    domain = extract_domain(mail_from_address)
    if domain is None:
        raise ValueError("发件地址无法解析出邮件域，不能生成出站 Message-ID")
    return f"<{row_id}@{domain}>"
