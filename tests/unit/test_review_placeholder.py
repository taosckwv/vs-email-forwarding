#!/usr/bin/env python3
"""审查接口与空实现占位（`providers/review/`）的单测（`AC-25` / `NFR-6`）。

覆盖：① `base.py` 与 `always_pass.py` 开头注释同时出现「空实现」「永远通过」
两关键词，且 `docs/security.md` 的安全边界一节可检索到该标注（`AC-25` 原文
的三处落点）；② 输入对象字段恰为 `design.md` §4.2 固定的六字段；③ 空实现
行为：两个 `direction` 均返回 `pass`。

期望值来源（`docs/testing.md` R2，规格推导字面量）：

* 关键词「空实现」「永远通过」：`NFR-6` / `AC-25` 明列的两词；
* 六字段清单：`design.md` §4.2 输入字段表逐行；
* `direction` 取值 `outbound` / `inbound`、判定 `pass`：`design.md` §4.2
  输入表与「一期空实现恒返回 `pass`」；`message_id` 为主键（UUID）。
"""

from __future__ import annotations

import uuid
from dataclasses import fields
from pathlib import Path

import pytest

from vs_email_forwarding.providers.review.always_pass import AlwaysPassReviewer
from vs_email_forwarding.providers.review.base import ReviewMessage


def _repo_root() -> Path:
    """定位真仓库根：从本文件逐级向上找 ``docs/security.md``。

    mutmut 会把源树拷进 ``mutants/`` 沙箱再跑同一套单测，沙箱里只有
    pyproject.toml / src / tests / uv.lock、**没有 docs/**——按固定层级
    （``parents[2]``）取根会在沙箱里指向不存在的路径（阶段 3 门禁，当时
    任务号 ``T-17``，曾因此失败）。逐级向上找则两种语境都成立：常规运行
    **第三个候选**（``parents[2]``，即真仓库根）就命中，沙箱里同一个锚多走
    **一级**（``parents[3]``）同样落回真根，标注断言因此读的是**未变异**的
    真文件，不被变异运行误伤。找不到即说明测试脱离了仓库检出，直接失败。

    本函数**有意自包含**、不抽公共 helper：三处同名函数的取根需求不同——本函数锤
    ``docs/security.md``，`tests/unit/test_openapi_contract.py` 与
    `tests/unit/test_constants_consistency.py` 锤 ``docs/api-contract.yaml``（后者还另需
    `migrations/`）；三处都为在 mutmut 沙箱内**零导入耦合**而各自实现（只用标准库
    `pathlib`），这是沙箱里唯一稳的取根方式（**本函数是先例**，另两处同名函数的注释即指向
    这里）。
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "docs" / "security.md").is_file():
            return candidate
    raise AssertionError("未找到仓库根：docs/security.md 不在本文件的任何上级目录")


_REPO_ROOT = _repo_root()
_BASE_PY = _REPO_ROOT / "src" / "vs_email_forwarding" / "providers" / "review" / "base.py"
_ALWAYS_PASS_PY = (
    _REPO_ROOT / "src" / "vs_email_forwarding" / "providers" / "review" / "always_pass.py"
)
_SECURITY_MD = _REPO_ROOT / "docs" / "security.md"

#: `NFR-6` / `AC-25` 明列的两个关键词。
_KEYWORDS = ("空实现", "永远通过")


def _leading_comment(path: Path) -> str:
    """取文件开头部分（shebang 与模块 docstring，即 `AC-25` 所指「开头注释」）。"""
    return path.read_text(encoding="utf-8").split("from __future__", 1)[0]


def _security_boundary_section() -> str:
    """取 `docs/security.md` 的「本项目特有的安全边界」一节（到下一个二级标题前）。"""
    text = _SECURITY_MD.read_text(encoding="utf-8")
    start = text.index("## 本项目特有的安全边界")
    end = text.find("\n## ", start + 1)
    return text[start:end]


def test_empty_implementation_annotated() -> None:
    # AC-25：接口文件与空实现文件的开头注释均出现「空实现」与「永远通过」。
    for path in (_BASE_PY, _ALWAYS_PASS_PY):
        head = _leading_comment(path)
        for keyword in _KEYWORDS:
            assert keyword in head, f"{path.name} 开头注释缺关键词「{keyword}」"


def test_security_boundary_section_mentions_annotation() -> None:
    # AC-25：docs/security.md 的安全边界一节能通过关键词检索到该标注。
    section = _security_boundary_section()
    for keyword in _KEYWORDS:
        assert keyword in section, f"docs/security.md 安全边界一节缺关键词「{keyword}」"


def test_review_message_fields_exactly_as_contract() -> None:
    # design.md §4.2 输入字段表：恰好六字段——direction / message_id /
    # recipient / subject / body / body_format，没有多余载体。
    assert tuple(field.name for field in fields(ReviewMessage)) == (
        "direction",
        "message_id",
        "recipient",
        "subject",
        "body",
        "body_format",
    )


@pytest.mark.parametrize("direction", ["outbound", "inbound"])
async def test_always_pass_returns_pass_for_both_directions(direction: str) -> None:
    # design.md §4.2：一期空实现恒返回 pass（忽略全部输入字段，两个方向一致）。
    message = ReviewMessage(
        direction=direction,
        message_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        recipient="peer@example.com",
        subject="周报",
        body="本周完成阶段3核心模块。",
        body_format="text",
    )
    assert await AlwaysPassReviewer().review(message) == "pass"
