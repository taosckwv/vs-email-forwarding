#!/usr/bin/env python3
"""HTTP 请求 / 响应模型。

职责：定义对外的 Pydantic 模型（投递请求、状态查询、回复查询等）与字段校验规则。

不放：ORM 模型（在 ``vs_email_forwarding.db``）。

当前状态：字段与状态枚举取值**尚未定案**，见 ``docs/open-questions.md``，
因此本模块暂无实现。
"""
