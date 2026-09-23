import asyncio
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from desk.artifacts.store import append_artifact
from desk.collectors.base import CollectResult
from desk.collectors.ingest import ingest
from desk.collectors.release_calendar import ReleaseCalendar, parse_fomc_meetings
from desk.watch.calendar import CalendarEvent, MarketCalendar, load_calendar_config
from tests.probe import ProbeArtifact

FOMC_PAGE = """
<h4>2027 FOMC Meetings</h4>
<div class="fomc-meeting__month col-xs-5"><strong>January</strong></div>
<div class="fomc-meeting__date col-xs-4">26-27</div>
<div class="fomc-meeting__month col-xs-5"><strong>Apr/May</strong></div>
<div class="fomc-meeting__date col-xs-4">30-1*</div>
<h4>2026 FOMC Meetings</h4>
<div class="fomc-meeting__month col-xs-5"><strong>September</strong></div>
<div class="fomc-meeting__date col-xs-4">15-16*</div>
<div class="fomc-meeting__month col-xs-5"><strong>August</strong></div>
<div class="fomc-meeting__date col-xs-4">22 (notation vote)</div>
<div class="fomc-meeting__month col-xs-5"><strong>October</strong></div>
<div class="fomc-meeting__date col-xs-4">27-28</div>
"""


def test_parse_fomc_meetings() -> None:
    assert parse_fomc_meetings(FOMC_PAGE) == [
        date(2026, 9, 16),
        date(2026, 10, 28),
        date(2027, 1, 27),
        date(2027, 5, 1),
    ]


def test_release_calendar_collects_all_sources() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "federalreserve" in request.url.host:
            return httpx.Response(200, text=FOMC_PAGE)
        assert request.url.params["api_key"] == "FREDKEY"
        release = request.url.params["release_id"]
        day = "2026-10-14" if release == "10" else "2026-10-02"
        return httpx.Response(200, json={"release_dates": [{"date": day}]})

    config = load_calendar_config()
    collector = ReleaseCalendar(
        config, MarketCalendar(config), "FREDKEY", transport=httpx.MockTransport(handler)
    )
    result = asyncio.run(collector.collect())
    kinds = {event.kind for event in result.calendar_events}
    assert {"fomc", "cpi", "jobs", "eia_petroleum", "eia_natural_gas", "cftc_cot"} <= kinds
    cpi = next(e for e in result.calendar_events if e.kind == "cpi")
    assert cpi.at == datetime(2026, 10, 14, 12, 30, tzinfo=UTC)  # 08:30 ET
    assert result.errors == []


def test_calendar_refresh_replaces_moved_events(db_conn: Connection) -> None:
    soon = datetime.now(UTC) + timedelta(days=10)
    original = CalendarEvent("testkind:a", "testkind", "Test", soon, "test")
    moved = CalendarEvent("testkind:b", "testkind", "Test", soon + timedelta(days=1), "test")
    ingest(db_conn, CollectResult(calendar_events=[original], calendar_kinds=["testkind"]))
    ingest(db_conn, CollectResult(calendar_events=[moved], calendar_kinds=["testkind"]))
    keys = (
        db_conn.execute(text("SELECT event_key FROM calendar_events WHERE kind = 'testkind'"))
        .scalars()
        .all()
    )
    assert keys == ["testkind:b"]


def test_artifact_shift_must_exist(db_conn: Connection) -> None:
    orphan = ProbeArtifact(produced_by="test", runtime_ms=0, note="x", shift_id=uuid4())
    with pytest.raises(IntegrityError):
        append_artifact(db_conn, orphan)
