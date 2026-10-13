#!/usr/bin/env python3
"""重试退避算术与 SMTP 返回码分类的纯逻辑单测。

覆盖：退避表的差值口径（`AC-9`）、第 4 次不推、越界尝试数与裸时间的拒绝、
4xx / 5xx 分类及其区间边界两侧（399 / 400、499 / 500、599 / 600）、`AC-21`
的最坏路径纯算术（`FR-15` 的 180 秒上界）。

期望值来源（`docs/testing.md` R2 / R11——约束取值只引用 constants，不复述数字）：

* 退避差值与尝试上限：`vs_email_forwarding.constants`（运行期唯一真源，取值
  定义在 `docs/research/02-runtime-and-scheduling.md`）；
* 分类区间边界 399 / 400 / 499 / 500 / 599 / 600 与归类结果：
  `specs/0001-email-forwarding-service/design.md` §2.4 的「4xx 可重试
  （transient）、5xx 不可重试（permanent）」——SMTP 失败码类别是协议事实；
* 180 秒上界：`spec.md` 的 `FR-15`（`AC-21` 最坏路径 = 4 × 发信超时上界 +
  退避表全用满 ≤ 180 秒）；超时上界取 `config.Settings` 字段声明的默认值
  （不经 `.env` / 环境变量，保证算术输入确定）；
* `now` 与绝对时刻：手工构造的 aware UTC `datetime` 字面量。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from vs_email_forwarding import constants
from vs_email_forwarding.config import Settings
from vs_email_forwarding.core.retry import classify_smtp_code, next_attempt_at

#: 认领事务取得的时刻（aware UTC 字面量；plan.md §5.2：时间由调用方传入，
#: 纯逻辑不自行取时）。
_T0 = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize("attempt", [1, 2, 3])
def test_next_attempt_at_first_three_claims_push_backoff(attempt: int) -> None:
    # plan.md §5.2 / AC-9：认领第 n 次推 now + backoff[n]，
    # 差值即退避表该档的秒数。
    assert (next_attempt_at(_T0, attempt) - _T0).total_seconds() == (
        constants.RETRY_BACKOFF_SECONDS[attempt]
    )


def test_next_attempt_at_fourth_claim_does_not_push() -> None:
    # plan.md §5.2：第 4 次（上限）不推退避——该行只剩终态一条路。
    assert next_attempt_at(_T0, constants.MAX_ATTEMPTS) == _T0


@pytest.mark.parametrize("attempt", [0, 5])
def test_next_attempt_at_rejects_attempt_out_of_range(attempt: int) -> None:
    # 合法认领计数从 1 起、封顶 MAX_ATTEMPTS；越界即调用方契约违规，不兜底。
    with pytest.raises(ValueError, match="尝试次数"):
        next_attempt_at(_T0, attempt)


def test_next_attempt_at_rejects_naive_now() -> None:
    # AGENTS.md §7：时间一律带时区；裸 datetime 进纯逻辑直接拒绝。
    with pytest.raises(ValueError, match="带时区"):
        next_attempt_at(datetime(2026, 1, 15, 12, 0, 0), 1)


@pytest.mark.parametrize("code", [400, 499])
def test_classify_smtp_code_4xx_is_transient(code: int) -> None:
    # design.md §2.4：4xx（含下沿 400、上沿 499）是对端临时故障 → transient。
    assert classify_smtp_code(code) == "transient"


@pytest.mark.parametrize("code", [500, 599])
def test_classify_smtp_code_5xx_is_permanent(code: int) -> None:
    # design.md §2.4：5xx（含下沿 500、上沿 599）是对端永久拒绝 → permanent。
    assert classify_smtp_code(code) == "permanent"


@pytest.mark.parametrize("code", [399, 600])
def test_classify_smtp_code_outside_failure_ranges_rejected(code: int) -> None:
    # 399（4xx 之下）与 600（5xx 之上）都不是 SMTP 失败码——成功类 2xx / 3xx
    # 不该进失败分类器，进来了即调用方契约违规，直接失败、不默认当作可重试。
    with pytest.raises(ValueError, match="不在失败码区间"):
        classify_smtp_code(code)


def test_worst_case_attempts_within_180s() -> None:
    # AC-21 / FR-15：最坏路径 = 4 × 发信超时上界 + 退避表全用满（15+30+60）
    # ≤ 180 秒。超时上界取 Settings 字段声明的默认值（绕开 .env 与环境变量，
    # 保证算术输入确定）；180 是 spec.md FR-15 的上界。
    smtp_timeout_seconds = Settings.model_fields["smtp_timeout_seconds"].default
    worst_case_seconds = constants.MAX_ATTEMPTS * smtp_timeout_seconds + sum(
        constants.RETRY_BACKOFF_SECONDS.values()
    )
    assert worst_case_seconds <= 180
