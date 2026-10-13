#!/usr/bin/env python3
"""审查实现的工厂：依赖覆盖 / monkeypatch 的固定替换点（`design.md` §4.2，`plan.md` §2.3）。

需要审查器的代码不直接实例化具体实现，一律经本工厂取用；一期恒返回
**永远通过的空实现** `AlwaysPassReviewer`（真实审查能力尚未定案，见
`docs/open-questions.md`）。将来接入真实审查时，「按配置选取实现」的展开
落点在本工厂，属新决策（按 `AGENTS.md` §6 更新 `design.md`）。

**一期不存在可以关掉某条流（尤其审查流）的运行时配置**（`design.md` §4.2）：
`config.py` 没有审查实现的选取项，本工厂也不读任何配置——有意为之。审查是
每封邮件放行前的固定环节（`FR-3`），不提供「跳过审查」的开关。

测试替换的位置固定在本模块：阶段 5 集成夹具 `fake_review` 用依赖覆盖 /
monkeypatch 把本工厂换成「恒 `pass` / 恒 `reject` / 抛异常」三种模式的假实现
（`AC-6` / `AC-12` 依赖它，见 `plan.md` §7.2 的夹具表）。因此调用方应在**运行期**
调用本函数取用审查器，不要在 import 期绑定其返回值——这是替换点能生效的前提。
"""

from __future__ import annotations

from vs_email_forwarding.providers.review.always_pass import AlwaysPassReviewer
from vs_email_forwarding.providers.review.base import Reviewer

__all__ = ["create_reviewer"]


def create_reviewer() -> Reviewer:
    """返回一期审查实现：**永远通过**的空实现（`design.md` §4.2）。

    一期没有按实现选取的配置项；将来展开真实实现的选取逻辑（读哪个配置、
    有哪些实现）时，按 `AGENTS.md` §6 作为新决策落回本模块——但「关掉
    审查流」的运行时开关仍然不得出现。
    """
    return AlwaysPassReviewer()
