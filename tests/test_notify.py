import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import Connection

from desk.front_office.notify import (
    NtfyClient,
    decide_urgent,
    load_notify_config,
    record_push,
    urgent_counts,
)

NY = ZoneInfo("America/New_York")
URGENT = load_notify_config().urgent


def ny(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 23, hour, minute, tzinfo=NY).astimezone(UTC)


@pytest.mark.parametrize(
    ("moment", "allowed"),
    [(ny(6, 59), False), (ny(7, 0), True), (ny(20, 59), True), (ny(21, 0), False), (ny(2), False)],
)
def test_urgent_window(moment: datetime, allowed: bool) -> None:
    assert decide_urgent(URGENT, moment, NY, 0, 0).allowed is allowed


def test_urgent_limits() -> None:
    assert not decide_urgent(URGENT, ny(12), NY, URGENT.max_per_hour, 0).allowed
    assert "daily" in decide_urgent(URGENT, ny(12), NY, 0, URGENT.max_per_day).reason
    assert decide_urgent(URGENT, ny(12), NY, URGENT.max_per_hour - 1, 0).allowed


def test_stale_alert_is_held() -> None:
    now = ny(12)
    fresh = decide_urgent(URGENT, now, NY, 0, 0, alert_time=now - timedelta(minutes=5))
    stale = decide_urgent(URGENT, now, NY, 0, 0, alert_time=now - timedelta(minutes=90))
    assert fresh.allowed
    assert not stale.allowed and stale.reason.startswith("stale")


def test_disabled_urgent_is_held() -> None:
    off = URGENT.model_copy(update={"enabled": False})
    assert decide_urgent(off, ny(12), NY, 0, 0).reason == "urgent pushes are disabled"


def test_urgent_counts_only_sent_urgent(db_conn: Connection) -> None:
    now = datetime.now(UTC)
    before_hour, before_day = urgent_counts(db_conn, now, NY)
    common = {"ref_id": None, "title": "t", "message": "m", "click_url": None, "priority": "high"}
    record_push(db_conn, kind="urgent", status="sent", **common)
    record_push(db_conn, kind="urgent", status="held", reason="outside hours", **common)
    record_push(db_conn, kind="briefing", status="sent", **common)
    hour, day = urgent_counts(db_conn, now + timedelta(seconds=1), NY)
    assert (hour - before_hour, day - before_day) == (1, 1)


def test_ntfy_send_uses_headers_and_topic() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "x"})

    client = NtfyClient("https://ntfy.test/", "desk-abc", transport=httpx.MockTransport(handler))
    asyncio.run(
        client.send(
            title="Morning briefing ready",
            message="Tap to open.",
            priority="default",
            click_url="https://desk.example/briefs/1",
        )
    )
    request = seen[0]
    assert str(request.url) == "https://ntfy.test/desk-abc"
    assert request.headers["Title"] == "Morning briefing ready"
    assert request.headers["Click"] == "https://desk.example/briefs/1"
    assert request.content == b"Tap to open."
