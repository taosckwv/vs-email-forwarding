#!/usr/bin/env python3
"""出站接口契约的集成面：提交、状态查询、回复分页（T-38；`AC-1`~`AC-5` / `15`~`18` / `31` /
`33` / `35` / `36` / `41`）。

手法（`plan.md` §7.3 的 `tests/integration/test_api_contract.py` 一行）：**经 HTTP 打真实应用**
（`create_app()` + `httpx.ASGITransport`，不进 `lifespan`——进程装配与启动校验由
`test_startup_validation.py` 判、状态推进由用例显式跑流 tick），底下是**真实 PostgreSQL**
（T-33 夹具的隔离库与清表）与**真实进程内 SMTP/IMAP 替身**（`providers/` 的边界替身，
不是数据库 mock）。期望值一律取自契约与设计原文，**不调用被测实现的内部函数**：

* 状态取值与 `found` 口径：`docs/api-contract.yaml`（`SubmitMessageResponse.status`、
  `MessageStatusResponse.found` 的说明、`GET /v1/messages/{id}` 的「不得用 404 区分业务语义」）；
* 六态与单调推进：`design.md` §2.3 的状态机（本文件把设计原文里的六态写成字面量）；
* 时间形态：契约 `Timestamp` 的「UTC RFC 3339 固定六位小数 + `Z`」——用正则与
  `strptime`（格式字面量写在本文件），并与库中列值逐个对齐；
* 上限来源：`design.md` §2.6 的「主题与正文上限都是配置项」——用例**改配置**再判边界，
  而不是把默认值 500 / 1048576 抄进断言。

落点分工（避免同一件事测两遍）：

* `From` / `Reply-To` 的**投递面**（真正写进 SMTP 的信封与头）在
  `tests/integration/test_send_flow.py`（T-28）；本文件只判**接口面**：调用方塞进来的发件人 /
  回复地址字段一律不生效、不落库（`AC-4` / `AC-5` 的调用方不可指定这一半）；
* 回复排序键的**仓储面**（keyset 谓词、并发插入）在 `tests/integration/test_repositories.py`；
  本文件判**游标面**：`next_cursor` 能原样回传、逐页取完、新回复不挤动已取过的页。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa

from vs_email_forwarding.db.models import Message, PendingMessage
from vs_email_forwarding.flows.fetch_inbound import fetch_inbound_tick
from vs_email_forwarding.flows.review_outbound import review_outbound_tick
from vs_email_forwarding.flows.send_email import send_email_tick
from vs_email_forwarding.main import create_app
from vs_email_forwarding.providers.imap.base import FetchedPart

pytestmark = pytest.mark.integration

#: 地址一律 `example.com` 占位（`AGENTS.md` §8 第 4 条：仓库里不写真实地址）。
_RECIPIENT: Final = "user-slot@example.com"
_CALLER_FROM: Final = "caller-from-slot@example.com"
_CALLER_REPLY_TO: Final = "caller-reply-slot@example.com"

#: 契约 `Timestamp` 的形状：UTC、RFC 3339、固定六位小数、以 `Z` 结尾。
_TIMESTAMP_PATTERN: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_TIMESTAMP_FORMAT: Final = "%Y-%m-%dT%H:%M:%S.%fZ"

#: 契约 `after` 的形状：`<received_at>,<UUID>`（设计 §4.1 的 keyset 游标）。
_CURSOR_PATTERN: Final = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z,[0-9a-f]{8}-[0-9a-f]{4}"
    r"-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

#: 设计 §2.3 的单调推进序：同一封邮件的状态取值只允许沿此序前进（拒审从审查中分出，属终局）。
_STATE_RANK: Final = {
    "submitted": 0,
    "reviewing": 1,
    "rejected": 1,
    "sending": 2,
    "sent": 3,
    "failed": 3,
}

#: `AC-41` 的规模：250 封回复、每页 100（页大小是配置项，用例显式传 `limit` 固定下来）。
_TOTAL_REPLIES: Final = 250
_PAGE_SIZE: Final = 100

#: 退避上界（`AGENTS.md` §8 第 7 条：15 / 30 / 60 秒，最多 3 次重试）——把时刻推到上界之后即可
#: 让被退避的行重新到点，用例因此不靠 sleep。
_BACKOFF_CEILING_SECONDS: Final = 61


@pytest.fixture
async def api() -> AsyncIterator[httpx.AsyncClient]:
    """真实应用 + ASGI 传输的异步客户端（不起监听端口、不进 `lifespan`）。"""
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


def _payload(**overrides: Any) -> dict[str, Any]:
    """一份形状合法的提交请求体（契约 `SubmitMessageRequest`：三个必填 + 一个可选格式）。"""
    body: dict[str, Any] = {"recipient": _RECIPIENT, "subject": "主题", "body": "正文"}
    body.update(overrides)
    return body


def _parse_timestamp(text: Any) -> datetime:
    """按契约 `Timestamp` 的形态解析一个时刻文本，形状不符即在本行先红。"""
    assert isinstance(text, str), f"时刻不是字符串：{text!r}"
    assert _TIMESTAMP_PATTERN.match(text), f"时刻形态不符合契约（六位小数 + Z）：{text!r}"
    return datetime.strptime(text, _TIMESTAMP_FORMAT).replace(tzinfo=UTC)


async def _get_status(api: httpx.AsyncClient, message_id: str) -> dict[str, Any]:
    response = await api.get(f"/v1/messages/{message_id}")
    assert response.status_code == 200, response.text
    return dict(response.json())


async def _get_replies(api: httpx.AsyncClient, message_id: str, **params: Any) -> dict[str, Any]:
    response = await api.get(f"/v1/messages/{message_id}/replies", params=params)
    assert response.status_code == 200, response.text
    return dict(response.json())


async def _count(session_factory: Any, table: sa.Table) -> int:
    async with session_factory() as session:
        return int(await session.scalar(sa.select(sa.func.count()).select_from(table)))


async def _columns_of(session: Any, row: Any) -> dict[str, Any]:
    """把一行读成**显式列投影**（不伸进实例的 `__dict__`）。

    实例字典里除列之外还有 `_sa_instance_state` 之类的私有键，其形态是 ORM 的实现细节；按
    `__table__.columns` 投影取的是同一份库内可观察值。先 `refresh` 一次保证列已加载——异步会话上
    对未加载属性取值会触发惰性加载（`MissingGreenlet`），这里不给它这个机会。
    """
    await session.refresh(row)
    return {column.key: getattr(row, column.key) for column in row.__table__.columns}


async def _pending_row(session_factory: Any, message_id: uuid.UUID) -> dict[str, Any] | None:
    async with session_factory() as session:
        row = (
            await session.execute(sa.select(PendingMessage).where(PendingMessage.id == message_id))
        ).scalar_one_or_none()
        return None if row is None else await _columns_of(session, row)


async def _due_at(session_factory: Any, message_id: str) -> datetime:
    """刚提交那行的到点时刻（流 tick 的注入时刻取它）。

    生产的提交路径用真实当前时刻建行，用例若自己先取一次时间，就会写出「tick 时刻恰好晚于
    提交时刻」这种靠真实时间推进的巧合；这里直接读该行自己的 `next_attempt_at`，
    「到点」因此是结构事实而不是运气（`plan.md` §7.2：不靠 sleep）。
    """
    row = await _pending_row(session_factory, uuid.UUID(message_id))
    assert row is not None, "刚提交的邮件必须有一行待审行"
    due = row["next_attempt_at"]
    assert isinstance(due, datetime)
    return due


async def _message_row(session_factory: Any, message_id: uuid.UUID) -> dict[str, Any] | None:
    async with session_factory() as session:
        row = (
            await session.execute(sa.select(Message).where(Message.id == message_id))
        ).scalar_one_or_none()
        return None if row is None else await _columns_of(session, row)


async def _seed_outbound(
    session_factory: Any, *, status: str = "sent", now: datetime
) -> tuple[uuid.UUID, str]:
    """直接造一封已过审的出站邮件（用例造态：不靠真实时间推进，`plan.md` §7.2）。"""
    message_id = uuid.uuid4()
    header = f"<{message_id}@example.com>"
    async with session_factory() as session:
        session.add(
            Message(
                id=message_id,
                direction="outbound",
                status=status,
                message_id=header,
                recipient=_RECIPIENT,
                subject="出站主题",
                body="出站正文",
                body_format="text",
                attempt_count=0,
                next_attempt_at=now,
                created_at=now,
                sent_at=now if status == "sent" else None,
                reviewed_at=now,
            )
        )
        await session.commit()
    return message_id, header


async def _seed_replies(
    session_factory: Any,
    rows: Sequence[tuple[uuid.UUID, datetime]],
    *,
    parent_id: uuid.UUID,
    first_uid: int = 1,
) -> None:
    """把一批入站回复行写进 `messages`（`direction = inbound`、`received_at` 即排序键）。

    入站行按 `design.md` §2.2 的列约束**不带 `message_id`**（那条 `Message-ID` 属于被回复的出站
    行），关联只靠 `parent_id`；入站三列（`mailbox` / `uidvalidity` / `uid`）必须齐备，
    且 `(mailbox, uidvalidity, uid)` 全局唯一——分批写入时用 `first_uid` 接着上一批的号往下排。
    """
    async with session_factory() as session:
        for index, (reply_id, received_at) in enumerate(rows):
            session.add(
                Message(
                    id=reply_id,
                    direction="inbound",
                    status="received",
                    message_id=None,
                    parent_id=parent_id,
                    recipient=_RECIPIENT,
                    subject=f"回复 {index}",
                    body=f"回复正文 {index}",
                    body_format="text",
                    mailbox="INBOX",
                    uidvalidity=1,
                    uid=first_uid + index,
                    attempt_count=0,
                    next_attempt_at=received_at,
                    created_at=received_at,
                    received_at=received_at,
                )
            )
        await session.commit()


def _dsn_sample(imap_fake: Any, *, original_message_id: str, uid: int = 1) -> None:
    """往信箱里放一封退信：`multipart/report` + `delivery-status` + 内嵌原邮件（`design.md` §3.3）。"""
    imap_fake.add_message(
        uid=uid,
        content_type="multipart/report",
        headers={"from": "mailer-daemon@example.com"},
        parts=(
            FetchedPart(
                content_type="message/delivery-status",
                payload=None,
                dsn_status="5.1.1",
                embedded_message_id=None,
            ),
            FetchedPart(
                content_type="message/rfc822",
                payload=None,
                dsn_status=None,
                embedded_message_id=original_message_id,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# `AC-1` ~ `AC-5`：提交
# ---------------------------------------------------------------------------


async def test_submit_then_query(api: httpx.AsyncClient, session_factory: Any) -> None:
    """`AC-1`：提交返回 2xx + 邮件 ID，随后按该 ID 立即查得「已提交」。"""
    response = await api.post("/v1/messages", json=_payload())
    assert response.status_code == 200, response.text
    submitted = response.json()
    assert set(submitted) == {"id", "status"}
    assert submitted["status"] == "submitted"
    message_id = uuid.UUID(submitted["id"])

    status = await _get_status(api, submitted["id"])
    assert status["id"] == submitted["id"]
    assert status["found"] is True
    assert status["status"] == "submitted"
    # 提交时刻必须等于库里那行的 `created_at`（30 天保留期自此时起算，`design.md` §2.5）。
    row = await _message_row(session_factory, message_id)
    assert row is None, "提交阶段只落待审行（design.md §3.1），不该已经有 messages 行"
    pending = await _pending_row(session_factory, message_id)
    assert pending is not None and pending["attempt_count"] == 0
    assert _parse_timestamp(status["created_at"]) == pending["created_at"].astimezone(UTC)
    assert status["sent_at"] is None and status["reviewed_at"] is None


async def test_missing_recipient_422(api: httpx.AsyncClient, session_factory: Any) -> None:
    """`AC-2`：缺收件人 → 422，响应体点名 `recipient` 字段，且**不落库**。"""
    before = await _count(session_factory, PendingMessage.__table__)
    payload = _payload()
    del payload["recipient"]

    response = await api.post("/v1/messages", json=payload)

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "recipient" in response.text
    assert any("recipient" in entry.get("loc", []) for entry in detail), detail
    assert await _count(session_factory, PendingMessage.__table__) == before


@pytest.mark.parametrize(
    "recipient",
    [["a@example.com", "b@example.com"], "a@example.com,b@example.com"],
    ids=["array", "comma"],
)
async def test_two_recipients_422(
    api: httpx.AsyncClient, session_factory: Any, recipient: Any
) -> None:
    """`AC-3`：数组形态与逗号分隔形态的多收件人都 → 422，且都不落库。"""
    before = await _count(session_factory, PendingMessage.__table__)

    response = await api.post("/v1/messages", json=_payload(recipient=recipient))

    assert response.status_code == 422, response.text
    assert await _count(session_factory, PendingMessage.__table__) == before


async def test_caller_sender_fields_are_ignored(
    api: httpx.AsyncClient, session_factory: Any
) -> None:
    """`AC-4` / `AC-5` 的接口面：调用方塞的发件人 / 回复地址一律不生效、不落库。

    投递面（真正写进 SMTP 的 `From` 恒取配置、`Reply-To` 恒等于被拉取信箱）由
    `tests/integration/test_send_flow.py` 判；本用例只钉「接口收不下这个输入」，
    与契约 `POST /v1/messages` 的「同名未知字段一律忽略、不报错」一致。
    """
    response = await api.post(
        "/v1/messages",
        json=_payload(**{"from": _CALLER_FROM, "reply_to": _CALLER_REPLY_TO}),
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "submitted"
    pending = await _pending_row(session_factory, uuid.UUID(response.json()["id"]))
    assert pending is not None
    stored = " ".join(str(value) for value in pending.values() if isinstance(value, str))
    assert _CALLER_FROM not in stored
    assert _CALLER_REPLY_TO not in stored
    assert pending["recipient"] == _RECIPIENT


# ---------------------------------------------------------------------------
# `AC-15` / `AC-16` / `AC-41`：回复查询
# ---------------------------------------------------------------------------


async def test_replies_stable(api: httpx.AsyncClient, session_factory: Any) -> None:
    """`AC-15`：同一批回复连查两次，ID 序列与顺序完全一致。"""
    now = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)
    parent_id, _ = await _seed_outbound(session_factory, now=now)
    ids = [uuid.uuid4() for _ in range(3)]
    await _seed_replies(
        session_factory,
        [(ids[index], now + timedelta(seconds=index)) for index in range(3)],
        parent_id=parent_id,
    )

    first = await _get_replies(api, str(parent_id))
    second = await _get_replies(api, str(parent_id))

    assert first["found"] is True
    assert [reply["id"] for reply in first["replies"]] == [str(value) for value in ids]
    assert first["replies"] == second["replies"]
    assert first["next_cursor"] == second["next_cursor"] is None


async def test_replies_sorted_asc(api: httpx.AsyncClient, session_factory: Any) -> None:
    """`AC-16`：回复按到达时刻升序返回——入库顺序与到达顺序无关时也不变。"""
    now = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)
    parent_id, _ = await _seed_outbound(session_factory, now=now)
    # 故意逆序入库：期望值是按 received_at 排好的那份，与写入顺序无关。
    arrivals = [now + timedelta(seconds=offset) for offset in (0, 60, 120)]
    ids = [uuid.uuid4() for _ in range(3)]
    await _seed_replies(
        session_factory,
        list(zip(reversed(ids), reversed(arrivals), strict=True)),
        parent_id=parent_id,
    )

    page = await _get_replies(api, str(parent_id))

    assert [reply["id"] for reply in page["replies"]] == [str(value) for value in ids]
    received = [_parse_timestamp(reply["received_at"]) for reply in page["replies"]]
    assert received == sorted(arrivals)
    assert received == sorted(received)


async def test_reply_paging_with_250_rows(api: httpx.AsyncClient, session_factory: Any) -> None:
    """`AC-41`：251 封回复逐页取完且不重不漏；翻页途中到达的新回复不挤动已取过的页。

    期望值是**测试自己写进去的那批行**按 `(received_at, id)` 排好的序列（`design.md` §2.5 的
    确定性兜底），不是拿接口的第一次输出当基准。
    """
    base = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)
    parent_id, _ = await _seed_outbound(session_factory, now=base)
    rows = [(uuid.uuid4(), base + timedelta(seconds=index)) for index in range(_TOTAL_REPLIES)]
    await _seed_replies(session_factory, rows, parent_id=parent_id)

    first = await _get_replies(api, str(parent_id), limit=_PAGE_SIZE)
    first_ids = [reply["id"] for reply in first["replies"]]
    assert len(first_ids) == _PAGE_SIZE
    assert first["next_cursor"] is not None and _CURSOR_PATTERN.match(first["next_cursor"])

    # 新回复落在第 2 页的范围里（第 100 与第 101 行之间），第 1 页不该被它挤动。
    late_id = uuid.uuid4()
    await _seed_replies(
        session_factory,
        [(late_id, base + timedelta(seconds=_PAGE_SIZE - 1, milliseconds=500))],
        parent_id=parent_id,
        first_uid=_TOTAL_REPLIES + 1,
    )

    collected = list(first_ids)
    cursor = first["next_cursor"]
    pages = 1
    while cursor is not None:
        page = await _get_replies(api, str(parent_id), limit=_PAGE_SIZE, after=cursor)
        collected.extend(reply["id"] for reply in page["replies"])
        cursor = page["next_cursor"]
        pages += 1

    assert pages == 3, f"251 封按每页 100 应取 3 页（末页 51 封），实为 {pages}"
    assert len(collected) == len(set(collected)) == _TOTAL_REPLIES + 1
    # 期望序：测试自己写进去的 250 行按到达时刻升序，再把新回复插到第 100 行**之后**、
    # 第 101 行之前（它的 `received_at` 落在两者之间，也就是第 2 页的第一条）。
    expected = [str(reply_id) for reply_id, _ in rows]
    expected.insert(_PAGE_SIZE, str(late_id))
    assert collected == expected
    # 第 1 页取过的内容原样不变：keyset 谓词不会因为新行而回退或重复。
    assert collected[:_PAGE_SIZE] == first_ids


# ---------------------------------------------------------------------------
# `AC-17` / `AC-18` / `AC-36`：状态推进（用例显式跑流 tick）
# ---------------------------------------------------------------------------


async def test_three_phase_states(
    api: httpx.AsyncClient,
    session_factory: Any,
    integration_environment: Any,
    fake_review: Any,
    smtp_capture: Any,
    imap_fake: Any,
) -> None:
    """`AC-17`：已提交 → 已发出 → 发送失败三个阶段的状态互不相同，时刻与库值一致。"""
    response = await api.post("/v1/messages", json=_payload())
    assert response.status_code == 200, response.text
    message_id = response.json()["id"]
    # 流 tick 的注入时刻取「该行的到点时刻」：提交那一刻由生产代码取时，用例不猜它、
    # 也不靠「tick 时刻恰好晚于提交时刻」这种真实时间上的巧合（`plan.md` §7.2）。
    tick_now = await _due_at(session_factory, message_id)

    submitted = await _get_status(api, message_id)
    assert submitted["status"] == "submitted"

    fake_review.mode = "pass"
    await review_outbound_tick(session_factory, integration_environment, now=tick_now)
    await send_email_tick(session_factory, integration_environment, now=tick_now)
    sent = await _get_status(api, message_id)
    assert sent["status"] == "sent"
    assert smtp_capture.delivery_count == 1, "投递必须真的打到进程内 SMTP 替身上"

    row = await _message_row(session_factory, uuid.UUID(message_id))
    assert row is not None and row["status"] == "sent"
    assert _parse_timestamp(sent["sent_at"]) == row["sent_at"].astimezone(UTC)
    assert _parse_timestamp(sent["reviewed_at"]) == row["reviewed_at"].astimezone(UTC)

    # 首见信箱的那一轮按设计不拉任何邮件（游标初始化为 `UIDNEXT - 1`，`design.md` §3.4）：
    # 先登记信箱，再让退信到达，然后跑第二轮——这与线上「服务先看到信箱、退信后到」同序。
    await fetch_inbound_tick(session_factory, integration_environment)
    _dsn_sample(imap_fake, original_message_id=row["message_id"])
    tick_result = await fetch_inbound_tick(session_factory, integration_environment)
    failed = await _get_status(api, message_id)
    assert failed["status"] == "failed", tick_result

    states = {submitted["status"], sent["status"], failed["status"]}
    assert states == {"submitted", "sent", "failed"}, states
    after = await _message_row(session_factory, uuid.UUID(message_id))
    assert after is not None and after["sent_at"] == row["sent_at"], "退信不得抹掉已发出的事实"
    assert _parse_timestamp(failed["sent_at"]) == row["sent_at"].astimezone(UTC)


async def test_rejected_state_queryable(
    api: httpx.AsyncClient,
    session_factory: Any,
    integration_environment: Any,
    fake_review: Any,
    smtp_capture: Any,
) -> None:
    """`AC-18`：判定拒审后可查询到「拒审」，且该 ID 不出现在任何「已发出」结果里。"""
    response = await api.post("/v1/messages", json=_payload())
    message_id = response.json()["id"]
    tick_now = await _due_at(session_factory, message_id)

    fake_review.mode = "reject"
    await review_outbound_tick(session_factory, integration_environment, now=tick_now)

    rejected = await _get_status(api, message_id)
    assert rejected["found"] is True
    assert rejected["status"] == "rejected"
    assert rejected["sent_at"] is None
    # 「不出现在 sent 结果里」：不投递、也不留一封能被当成已发出的行。
    await send_email_tick(session_factory, integration_environment, now=tick_now)
    assert smtp_capture.delivery_count == 0
    assert (await _get_status(api, message_id))["status"] == "rejected"
    assert await _message_row(session_factory, uuid.UUID(message_id)) is None


async def test_state_monotonic(
    api: httpx.AsyncClient,
    session_factory: Any,
    integration_environment: Any,
    fake_review: Any,
    smtp_capture: Any,
) -> None:
    """`AC-36`：轮询到的取值序列不出现倒退，且至少观测到「审查中」与「投递中」。

    由用例显式推进（`plan.md` §7.3：不靠真实时间）：审查档位先用抛出档把待审行**留在库里**
    （认领即 `attempt_count = 1`，对外就是「审查中」），再把时刻推过退避上界让它过审
    （落地为「投递中」），最后一次投递 tick 推到「已发出」。
    """
    response = await api.post("/v1/messages", json=_payload())
    message_id = response.json()["id"]
    tick_now = await _due_at(session_factory, message_id)

    observed = [(await _get_status(api, message_id))["status"]]
    fake_review.mode = "raise"
    await review_outbound_tick(session_factory, integration_environment, now=tick_now)
    observed.append((await _get_status(api, message_id))["status"])

    later = tick_now + timedelta(seconds=_BACKOFF_CEILING_SECONDS)
    fake_review.mode = "pass"
    await review_outbound_tick(session_factory, integration_environment, now=later)
    observed.append((await _get_status(api, message_id))["status"])

    await send_email_tick(session_factory, integration_environment, now=later)
    observed.append((await _get_status(api, message_id))["status"])

    assert "reviewing" in observed and "sending" in observed, observed
    ranks = [_STATE_RANK[state] for state in observed]
    assert ranks == sorted(ranks), f"状态出现倒退：{observed}"
    assert observed == ["submitted", "reviewing", "sending", "sent"], observed


# ---------------------------------------------------------------------------
# `AC-31` / `AC-33` / `AC-35`：边界与 422
# ---------------------------------------------------------------------------


async def test_existing_without_replies(api: httpx.AsyncClient, session_factory: Any) -> None:
    """`AC-31`：邮件存在但尚无回复 → 200 + `found: true` + 空集合（不是错误）。"""
    now = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)
    parent_id, _ = await _seed_outbound(session_factory, now=now)

    page = await _get_replies(api, str(parent_id))

    assert page["found"] is True
    assert page["replies"] == []
    assert page["next_cursor"] is None
    # 提交后还没过审的行同样「存在」：状态查询与回复查询同一 ID 空间（design.md §4.1）。
    response = await api.post("/v1/messages", json=_payload())
    fresh = await _get_replies(api, response.json()["id"])
    assert fresh["found"] is True and fresh["replies"] == []


async def test_over_limit_422(
    api: httpx.AsyncClient, session_factory: Any, settings_override: Any
) -> None:
    """`AC-33`：超限 → 422 且不落库；上限**取自配置项**——把配置改小，边界随之移动。"""
    settings_override(MAX_SUBJECT_CHARS=5, MAX_BODY_BYTES=5)
    before = await _count(session_factory, PendingMessage.__table__)

    over_subject = await api.post("/v1/messages", json=_payload(subject="abcdef"))
    over_body = await api.post("/v1/messages", json=_payload(body="abcdef"))
    at_limit = await api.post("/v1/messages", json=_payload(subject="abcde", body="abcde"))

    assert over_subject.status_code == 422, over_subject.text
    assert over_body.status_code == 422, over_body.text
    # 正好卡在配置的上限上必须放行：把 500 / 1048576 写死在代码里的实现会在这里或上一行翻车。
    assert at_limit.status_code == 200, at_limit.text
    assert await _count(session_factory, PendingMessage.__table__) == before + 1


async def test_not_found_vs_no_replies(api: httpx.AsyncClient, session_factory: Any) -> None:
    """`AC-35`：不存在的 ID 一律 200 + `found: false`，与「存在但无回复」可区分。"""
    missing = str(uuid.uuid4())

    status = await _get_status(api, missing)
    page = await _get_replies(api, missing)

    assert status["found"] is False and status["status"] is None
    assert status["created_at"] is None and status["sent_at"] is None
    assert page["found"] is False and page["replies"] == [] and page["next_cursor"] is None
    # 与 `AC-31` 的「存在但无回复」逐字段区分：只有 `found` 不同，状态码一律 200。
    now = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)
    parent_id, _ = await _seed_outbound(session_factory, now=now)
    existing = await _get_replies(api, str(parent_id))
    assert existing["replies"] == page["replies"] == []
    assert existing["found"] is True and page["found"] is False


async def test_unknown_recipient_shape_is_422(api: httpx.AsyncClient) -> None:
    """地址形态非法（无 `@` / 含空白）→ 422：与「缺收件人」是同一种错误面（契约的校验清单）。"""
    for recipient in ("no-at-sign", "has space@example.com"):
        response = await api.post("/v1/messages", json=_payload(recipient=recipient))
        assert response.status_code == 422, f"{recipient}: {response.text}"
