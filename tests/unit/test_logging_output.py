#!/usr/bin/env python3
"""`configure_logging` 的**实际输出**：时间戳、json / console 两条渲染分支、级别解析与回落、
uvicorn logger 的接管。

**为什么断言输出而不是配置**：`docs/testing.md` §4.5 的两分口径要求断言落在可观察结果上。
「`structlog.configure` 收到了什么 kwargs」这类断言在实现换一种写法后照样绿，盖不住本层对外
可见的东西——真正写到流上的日志文本与结构化字段。因此每个用例都用 `capsys` 采集被测函数写到
`sys.stderr` 的内容，再断言这些文本 / 结构化字段；只有需要观察「别的 handler 有没有也收到」
时才自建缓冲。

期望值来源（`docs/testing.md` R2）：

* `LOG_FORMAT` 的取值与语义（`json` 供容器采集、`console` 本地可读带颜色）：`.env.example`
  的注释与 `config.py` 的字段说明；
* `LOG_LEVEL` 的级别名与大小写不敏感：`.env.example` 与标准库 `logging` 的级别名约定；
  未知级别回落 `INFO`：本层对外契约（`logging.py` 模块 docstring：「无法识别时回落 INFO」）；
* 时间戳是 UTC 的 ISO8601：本层对外契约（「两条链路共用的 processor：时间戳统一为 UTC
  ISO8601」），`Z` 结尾是 UTC 的书写方式；
* uvicorn 三个 logger 名、以及它们「自带 handler + 不 propagate」的默认形态：uvicorn 日志配置
  的事实，本层把它们交给根 logger 以免重复输出（`logging.py` 模块 docstring）；
* 异常只渲染一份、`json` 保证 JSON-lines（每条记录恰好一行合法 JSON，traceback 落在
  `exception` 字段里）、`console` 事件一行 + traceback 一份：`design.md` §4.6。
"""

from __future__ import annotations

import io
import json
import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Final

import pytest
import structlog

from vs_email_forwarding.logging import configure_logging

#: uvicorn 默认日志配置里的三个 logger 名（本层逐个清 handler 并改 propagate）。
_UVICORN_LOGGER_NAMES: Final[tuple[str, ...]] = ("uvicorn", "uvicorn.error", "uvicorn.access")

#: UTC 的 ISO8601 时间戳：以 `Z` 结尾（本地时间不会带 `Z`）。
_UTC_ISO_TIMESTAMP: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")

#: 控制台渲染的着色序列（`ConsoleRenderer(colors=True)` 的产物）。
_ANSI_COLOR: Final = re.compile(r"\x1b\[[0-9;]*m")

#: 取时点与断言时点的容许偏差（秒）：只用来排除「时间戳写死或走了别的时区」，不测时钟精度。
_CLOCK_TOLERANCE_SECONDS: Final = 60

#: traceback 的头部文本：两种格式下都拿它当「渲染了一份异常」的可观察判据。
_TRACEBACK_HEADER: Final = "Traceback (most recent call last):"

#: 记一次异常的两条真实路径：structlog 的 `.exception()` 与标准库的 `error(exc_info=True)`。
#: 前者走 structlog 自己的调用链，后者走 `foreign_pre_chain`，覆盖两处入口。
_EXCEPTION_PATHS: Final[tuple[tuple[str, str, str], ...]] = (
    ("structlog", "probe_failed", "boom-structlog"),
    ("stdlib", "third_party_failed", "boom-stdlib"),
)

#: rich 面板的框线字符：`console` 的模式是 plain traceback（`design.md` §4.6），出现框线即
#: 说明 `ConsoleRenderer` 自己渲染了异常（链上那步 `format_exc_info` 不在发挥作用）。
_RICH_PANEL_CHARS: Final[tuple[str, ...]] = ("╭", "╰")


@pytest.fixture(autouse=True)
def _restore_process_logging_state() -> Iterator[None]:
    """逐例还原进程级日志状态（root handler 与 level、structlog 全局配置）。

    被测函数改的就是这两处全局状态，不还原会串到别的用例（R12：可乱序、可重复）。
    还原到「无 handler、级别 WARNING」这个中性起点，不假设任何用例的执行顺序。
    """
    yield
    structlog.reset_defaults()
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
    root_logger.setLevel(logging.WARNING)
    for name in _UVICORN_LOGGER_NAMES:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
        uvicorn_logger.setLevel(logging.NOTSET)


def _json_records(output: str) -> list[dict[str, object]]:
    """把采集到的文本逐行解析成结构化记录；不是 JSON 时就在这里失败。"""
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def test_json_format_writes_structured_json_per_event(capsys: pytest.CaptureFixture[str]) -> None:
    """`json` 分支：一条事件一行 JSON，结构化字段用事件名与受控字段承载。"""
    configure_logging("INFO", "json")
    structlog.get_logger("probe").info("probe_event", batch=3)

    records = _json_records(capsys.readouterr().err)

    assert [record["event"] for record in records] == ["probe_event"]
    assert records[0]["level"] == "info"
    assert records[0]["logger"] == "probe"
    assert records[0]["batch"] == 3


def test_json_timestamp_is_utc_iso8601(capsys: pytest.CaptureFixture[str]) -> None:
    """`json` 分支的时间戳：UTC、ISO8601、以 `Z` 结尾，且取的是当前时刻。"""
    configure_logging("INFO", "json")
    structlog.get_logger("probe").info("probe_event")

    timestamp = str(_json_records(capsys.readouterr().err)[0]["timestamp"])

    assert _UTC_ISO_TIMESTAMP.match(timestamp), f"时间戳不是 UTC ISO8601：{timestamp!r}"
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    assert parsed.utcoffset() == timedelta(0)
    assert abs((parsed - datetime.now(UTC)).total_seconds()) <= _CLOCK_TOLERANCE_SECONDS


def test_json_format_keeps_non_ascii_text_readable(capsys: pytest.CaptureFixture[str]) -> None:
    """`json` 分支不把中文转义：写进去的文本在输出里原样可见（日志要给人看，也要给采集器看）。"""
    configure_logging("INFO", "json")
    structlog.get_logger("probe").info("审查完成", note="中文正文片段")

    output = capsys.readouterr().err

    assert "审查完成" in output  # 没有被转义成 \\uXXXX
    record = _json_records(output)[0]
    assert record["event"] == "审查完成"
    assert record["note"] == "中文正文片段"


def test_console_format_writes_colored_text_not_json(capsys: pytest.CaptureFixture[str]) -> None:
    """`console` 分支：不是 JSON，而是带颜色、含事件名与绑定字段的文本（本地可读形态）。"""
    configure_logging("INFO", "console")
    structlog.get_logger("probe").info("probe_event", batch=3)

    output = capsys.readouterr().err

    with pytest.raises(json.JSONDecodeError):
        json.loads(output)  # 不是 JSONRenderer 的产物
    assert "\x1b[" in output  # config.py：console 是「本地可读（带颜色）」
    # 去掉着色序列后按文本读：事件名、级别与绑定字段都在（颜色只是装饰）。
    plain_output = _ANSI_COLOR.sub("", output)
    assert "probe_event" in plain_output
    assert "info" in plain_output
    assert "batch=3" in plain_output


@pytest.mark.parametrize("level", ["DEBUG", "debug", " DEBUG "])
def test_level_name_selects_requested_level(level: str, capsys: pytest.CaptureFixture[str]) -> None:
    """级别名按标准库约定大小写不敏感、并容忍 `.env` 里可能带的空白；DEBUG 必须真的放行。"""
    configure_logging(level, "json")
    structlog.get_logger("probe").debug("debug_event")

    assert [record["event"] for record in _json_records(capsys.readouterr().err)] == ["debug_event"]


def test_unknown_level_falls_back_to_info(capsys: pytest.CaptureFixture[str]) -> None:
    """未知级别名回落 INFO：INFO 事件照常输出、DEBUG 事件被挡住。"""
    configure_logging("no-such-level", "json")
    probe = structlog.get_logger("probe")
    probe.debug("debug_event")
    probe.info("info_event")

    assert [record["event"] for record in _json_records(capsys.readouterr().err)] == ["info_event"]


def test_configured_level_suppresses_lower_levels(capsys: pytest.CaptureFixture[str]) -> None:
    """配置的级别真的按标准库语义过滤输出：`WARNING` 下 INFO 事件不得写出来。"""
    configure_logging("WARNING", "json")
    probe = structlog.get_logger("probe")
    probe.info("info_event")
    probe.warning("warning_event")

    records = _json_records(capsys.readouterr().err)

    assert [record["event"] for record in records] == ["warning_event"]


@pytest.mark.parametrize("logger_name", _UVICORN_LOGGER_NAMES)
def test_uvicorn_logger_handlers_detached_and_propagated(
    logger_name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """uvicorn 的 logger：自带 handler 被清空（不重复输出），记录经根 logger 输出一次。"""
    own_output = io.StringIO()
    own_handler = logging.StreamHandler(own_output)
    own_handler.setFormatter(logging.Formatter("uvicorn 自带 handler：%(message)s"))
    uvicorn_logger = logging.getLogger(logger_name)
    uvicorn_logger.handlers = [own_handler]
    uvicorn_logger.propagate = False

    configure_logging("INFO", "json")
    uvicorn_logger.warning("uvicorn_event")

    assert own_output.getvalue() == ""  # 自带 handler 已摘掉
    records = _json_records(capsys.readouterr().err)
    assert [record["event"] for record in records] == ["uvicorn_event"]  # 输出恰好一次
    assert records[0]["logger"] == logger_name
    # 第三方记录同样经过本层的 processor 链：结构化字段与时间戳一个不少。
    assert {"event", "level", "logger", "timestamp"} <= set(records[0])


def test_existing_root_handlers_are_replaced(capsys: pytest.CaptureFixture[str]) -> None:
    """根 logger 上原有的 handler 被替换：旧 handler 不再输出，日志只写一次。"""
    existing_output = io.StringIO()
    existing_handler = logging.StreamHandler(existing_output)
    existing_handler.setFormatter(logging.Formatter("旧根 handler：%(message)s"))
    logging.getLogger().addHandler(existing_handler)

    configure_logging("INFO", "json")
    structlog.get_logger("probe").info("probe_event")

    assert existing_output.getvalue() == ""
    assert [record["event"] for record in _json_records(capsys.readouterr().err)] == ["probe_event"]


def _log_exception(path_name: str, event: str, marker: str) -> None:
    """按一条真实路径记一次异常：`structlog` 用 `.exception()`，`stdlib` 用 `error(exc_info=True)`。"""
    try:
        raise ValueError(marker)
    except ValueError:
        if path_name == "structlog":
            structlog.get_logger("probe").exception(event)
        else:
            logging.getLogger("third.party").error(event, exc_info=True)


@pytest.mark.parametrize("fmt", ["json", "console"])
@pytest.mark.parametrize(
    ("path_name", "event", "marker"),
    _EXCEPTION_PATHS,
    ids=[path_name for path_name, _, _ in _EXCEPTION_PATHS],
)
def test_exception_is_rendered_exactly_once(
    fmt: str, path_name: str, event: str, marker: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """异常只渲染一份（`design.md` §4.6）：`json` 保持 JSON-lines，`console` 只有一份 traceback。

    期望值来自 `design.md` §4.6 的两条对外承诺——`json` 每条记录恰好一行合法 JSON、异常只在该行的
    `exception` 字段里；`console` 事件一行 + 异常一份（plain traceback，不带框、不带第二份纯文本）。
    两个断言都落在**实际采集到的输出**上：`keep_exc_info=True` 时 `json` 会多出 4 行裸文本（逐行
    `json.loads` 就在这里失败），`console` 的 traceback 会渲染两份（计数变 2）；若把 console 链上的
    `format_exc_info` 删掉，`ConsoleRenderer` 会自己渲染成 rich 框（计数变 0、出现框线）。
    """
    configure_logging("INFO", fmt)
    _log_exception(path_name, event, marker)

    output = capsys.readouterr().err

    if fmt == "json":
        records = _json_records(output)  # 流里出现任何裸文本行都会在这里失败
        assert len(records) == 1, output
        assert records[0]["event"] == event
        assert records[0]["level"] == "error"
        assert f"ValueError: {marker}" in str(records[0]["exception"])
        assert str(records[0]["exception"]).startswith(_TRACEBACK_HEADER)
    else:
        plain = _ANSI_COLOR.sub("", output)
        assert plain.count(_TRACEBACK_HEADER) == 1, plain
        assert f"ValueError: {marker}" in plain
        assert event in plain
        # §4.6 第 2 条硬要求（console 链必须在 `ConsoleRenderer` 之前包含 `format_exc_info`）：
        # 漏掉那一步，异常就改由 `ConsoleRenderer` 自己渲染成 rich 框（带框线、带 locals），
        # 「渲染器只负责排版、两模式同源」当即不成立。
        assert not any(box in plain for box in _RICH_PANEL_CHARS), plain
