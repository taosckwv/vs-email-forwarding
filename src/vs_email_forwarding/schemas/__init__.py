#!/usr/bin/env python3
"""HTTP 请求 / 响应模型。

职责：定义对外的 Pydantic 模型（提交请求、状态查询、回复查询等）与字段校验规则。
字段、类型、必填与取值约束的**唯一真源**是 ``docs/api-contract.yaml``（OpenAPI 3.1）：
本目录按它落地模型，**不在别处重复字段定义**（``specs/0001-email-forwarding-service/plan.md`` §2.10）。

不放：ORM 模型（在 ``vs_email_forwarding.db``）。
"""
