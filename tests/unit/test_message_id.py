#!/usr/bin/env python3
"""出站 `Message-ID` 生成与发件地址取域的纯逻辑单测。

覆盖 `design.md` §3.1 的生成与取域规则，以及 §4.4 启动校验判据 2
（「`MAIL_FROM_ADDRESS` 能被解析出邮件域」）所依赖的失败路径——取不出域的
地址必须被判为不可解析（返回 `None`），`<uuid@>` 这类残缺值不允许被生成。

期望值来源（`docs/testing.md` R2，全部为手工推导的字面量）：

* 取域规则：`specs/0001-email-forwarding-service/design.md` §3.1——「域名部分
  取 `MAIL_FROM_ADDRESS` 里 `@` 之后的内容，若该配置带显示名则先取最后一个
  `<...>` 内的地址再取域」；
* 生成规则：同节——「形如 `<{UUID}@{发件域}>`：UUID 部分用该行的主键 `id`」；
* 失败路径：`design.md` §4.4 判据 2——「`<uuid@>` 这种残缺值必须在启动时就被
  拦住」，对应取域返回 `None`、生成函数对残缺地址抛 `ValueError`；
* 引号本地部：RFC 5322 §3.2.4（本地部分可写成带引号的字符串）与 §3.4.1
  （``"john@smith"@example.com`` 的本地部分是 ``john@smith``、域名是
  ``example.com``）——引号内允许出现 ``@``，所以**最后一个** ``@`` 才是
  本地部分与域名的分隔符。
"""

from __future__ import annotations

import uuid

import pytest

from vs_email_forwarding.core.message_id import build_message_id, extract_domain

#: design.md §2：三张邮件表的主键是应用生成的 UUIDv4；测试取全 1 的确定性 UUID 作输入，
#: 期望串按 str(UUID) 的小写规范形手工写出。
_ROW_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def test_extract_domain_standard_address_returns_after_at() -> None:
    # design.md §3.1：无显示名时，域名部分取地址里 @ 之后的全部内容。
    assert extract_domain("noreply@example.com") == "example.com"


def test_extract_domain_display_name_takes_angle_address() -> None:
    # design.md §3.1：带显示名时先取 <...> 内的地址再取域——不是拿整串去切 @
    # （整串切会带上结尾的 ">"，期望值不含它）。
    assert extract_domain("邮件中转服务 <noreply@relay.example.com>") == "relay.example.com"


def test_extract_domain_multiple_angle_parts_takes_last() -> None:
    # design.md §3.1：多个 <...> 时取最后一个内的地址——甲段的域是 one.example.com，
    # 期望取乙段的 two.example.com。
    assert extract_domain("甲 <a@one.example.com> 乙 <b@two.example.com>") == "two.example.com"


def test_extract_domain_without_at_returns_none() -> None:
    # design.md §4.4 判据 2：解析不出邮件域的地址必须可判别（None），
    # 启动校验据此拦住配置错误，不做猜测式兜底。
    assert extract_domain("noreply.example.com") is None


def test_extract_domain_empty_domain_returns_none() -> None:
    # design.md §4.4 判据 2 的边界另一侧：@ 之后为空（残缺值）同样取不出域。
    assert extract_domain("noreply@") is None


def test_extract_domain_quoted_local_part_splits_at_last_at() -> None:
    # RFC 5322 §3.2.4 / §3.4.1：本地部分可以是带引号的字符串，引号里允许出现
    # 原子不允许的字符（含 @）——所以分隔本地部分与域名的是**最后一个** @，
    # 引号里那个不是。design.md §3.1 的取域规则（@ 之后取域）由此定死：
    # 从该地址取出的域是 example.com，而不是把 smith"@example.com 当成域。
    assert extract_domain('"john@smith"@example.com') == "example.com"


def test_extract_domain_empty_string_returns_none() -> None:
    # design.md §4.4 判据 2：空配置不是「能取域的地址」——取不出域即 None，
    # 启动校验据此拒绝；空串不得被当成「域为空」而放行，也不得返回空串
    # （空串与 None 在后置校验里是两回事）。
    assert extract_domain("") is None


def test_build_message_id_formats_uuid_and_domain() -> None:
    # design.md §3.1：形如 <{UUID}@{发件域}>，UUID 部分即行主键 id（参数传入）。
    assert build_message_id(_ROW_ID, "noreply@example.com") == (
        "<11111111-1111-1111-1111-111111111111@example.com>"
    )


def test_build_message_id_with_display_name_uses_angle_domain() -> None:
    # design.md §3.1：生成走同一条取域路径——带显示名时用 <...> 内地址的域。
    assert build_message_id(_ROW_ID, "邮件中转服务 <noreply@relay.example.com>") == (
        "<11111111-1111-1111-1111-111111111111@relay.example.com>"
    )


def test_build_message_id_unparsable_domain_raises_value_error() -> None:
    # design.md §4.4：<uuid@> 这种残缺值不允许被生成——取不出域时显式失败，
    # 而不是拼出残缺的 Message-ID。异常信息只描述事实、不回显地址（AGENTS.md §8.4）。
    with pytest.raises(ValueError, match="无法解析出邮件域"):
        build_message_id(_ROW_ID, "残缺地址没有at也没有尖括号")
