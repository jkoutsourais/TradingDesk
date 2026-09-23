"""Ingest dedupe, buffer purge, append-only series, runner bookkeeping, gap detection."""

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from desk.artifacts.raw_record import RawRecord, content_hash
from desk.artifacts.store import append_artifact
from desk.collectors import health
from desk.collectors.base import CollectResult, SeriesObservation
from desk.collectors.ingest import ingest
from desk.collectors.purge import purge_raw_records
from desk.collectors.report import find_gaps
from desk.collectors.runner import CollectorRunner
from desk.config import CollectorSchedule, Window, load_schedule
from tests.probe import ProbeArtifact

NOW = datetime.now(UTC)


def raw(source_id: str, *, age_days: float = 0.0, unique: str | None = None) -> RawRecord:
    return RawRecord(
        produced_by="data.test",
        runtime_ms=0,
        created_at=NOW - timedelta(days=age_days),
        source="test.source",
        source_id=source_id,
        fetched_at=NOW - timedelta(days=age_days),
        payload={"text": source_id},
        content_hash=content_hash("test.source", source_id, unique or str(uuid4())),
    )


def observation(value: str, series_id: str | None = None) -> SeriesObservation:
    return SeriesObservation(
        source="fred",
        series_id=series_id or f"TEST{uuid4().hex[:6]}",
        period=date(2026, 9, 18),
        value=Decimal(value),
        unit=None,
        fetched_at=NOW,
    )


def test_ingest_dedupes_within_batch_and_against_existing(db_conn: Connection) -> None:
    first = raw("a", unique="same")
    duplicate = raw("a-again", unique="same")
    assert first.content_hash == content_hash("test.source", "a", "same")
    duplicate = duplicate.model_copy(update={"content_hash": first.content_hash})
    counts = ingest(db_conn, CollectResult(records=[first, duplicate, raw("b")]))
    assert (counts.records_added, counts.records_duplicate) == (2, 1)

    again = ingest(db_conn, CollectResult(records=[first]))
    assert (again.records_added, again.records_duplicate) == (0, 1)


def test_database_rejects_duplicate_content_hash(db_conn: Connection) -> None:
    record = raw("x")
    append_artifact(db_conn, record)
    clone = record.model_copy(update={"id": uuid4()})
    with pytest.raises(IntegrityError):
        append_artifact(db_conn, clone)


def test_observations_dedupe_and_revisions_add_rows(db_conn: Connection) -> None:
    series_id = f"TEST{uuid4().hex[:6]}"
    counts = ingest(db_conn, CollectResult(observations=[observation("4.12", series_id)]))
    assert counts.observations_added == 1
    again = ingest(db_conn, CollectResult(observations=[observation("4.12", series_id)]))
    assert again.observations_added == 0
    revised = ingest(db_conn, CollectResult(observations=[observation("4.15", series_id)]))
    assert revised.observations_added == 1


def test_observation_batch_counts_only_new_rows(db_conn: Connection) -> None:
    series_id = f"TEST{uuid4().hex[:6]}"
    batch = [observation("1.0", series_id), observation("2.0", series_id), observation("3.0")]
    assert ingest(db_conn, CollectResult(observations=batch)).observations_added == 3
    batch.append(observation("4.0", series_id))
    assert ingest(db_conn, CollectResult(observations=batch)).observations_added == 1


def test_series_observations_are_append_only(db_conn: Connection) -> None:
    ingest(db_conn, CollectResult(observations=[observation("1.0")]))
    with pytest.raises(DBAPIError, match="append-only"):
        db_conn.execute(text("UPDATE series_observations SET value = 2"))


def test_purge_removes_only_expired_uncited_records(db_conn: Connection) -> None:
    expired = raw("expired", age_days=8)
    cited = raw("cited", age_days=8)
    fresh = raw("fresh", age_days=1)
    for record in (expired, cited, fresh):
        append_artifact(db_conn, record)
    append_artifact(
        db_conn, ProbeArtifact(produced_by="test", runtime_ms=0, note="cites", parents=(cited.id,))
    )

    purge_raw_records(db_conn, retention_days=7)

    remaining = {
        row[0]
        for row in db_conn.execute(
            text("SELECT id FROM artifacts WHERE id = ANY(:ids)"),
            {"ids": [expired.id, cited.id, fresh.id]},
        )
    }
    assert remaining == {cited.id, fresh.id}


def test_raw_record_delete_without_purge_opt_in_is_rejected(db_conn: Connection) -> None:
    record = raw("protected", age_days=8)
    append_artifact(db_conn, record)
    with pytest.raises(DBAPIError, match="append-only"):
        db_conn.execute(text("DELETE FROM artifacts WHERE id = :id"), {"id": record.id})


def test_purge_opt_in_does_not_cover_other_kinds(db_conn: Connection) -> None:
    probe = ProbeArtifact(produced_by="test", runtime_ms=0, note="keep")
    append_artifact(db_conn, probe)
    db_conn.execute(text("SET LOCAL desk.purge = 'on'"))
    with pytest.raises(DBAPIError, match="append-only"):
        db_conn.execute(text("DELETE FROM artifacts WHERE id = :id"), {"id": probe.id})


# --- Runner bookkeeping (commits, so names are unique per test) ---------------------------


class StubCollector:
    def __init__(self, name: str, outcome: CollectResult | Exception) -> None:
        self.name = name
        self._outcome = outcome

    async def collect(self) -> CollectResult:
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome

    async def aclose(self) -> None:
        return None


def health_row(engine: Engine, name: str) -> dict[str, object]:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM collector_health WHERE collector = :c"), {"c": name}
        ).one()
        return dict(row._mapping)


def job_statuses(engine: Engine, name: str) -> list[str]:
    with engine.connect() as conn:
        return [
            r[0]
            for r in conn.execute(
                text("SELECT status FROM job_runs WHERE job = :j ORDER BY started_at"), {"j": name}
            )
        ]


@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_added"),
    [
        (CollectResult(), "ok", 0),
        (CollectResult(errors=["NVDA: GET https://x -> HTTP 500"]), "failed", 0),
        (RuntimeError("boom"), "failed", 0),
    ],
)
def test_runner_records_job_and_health(
    db_engine: Engine, outcome: CollectResult | Exception, expected_status: str, expected_added: int
) -> None:
    name = f"stub_{uuid4().hex[:8]}"
    if isinstance(outcome, CollectResult) and not outcome.errors:
        outcome = CollectResult(records=[raw("stored")])
        expected_added = 1
    with db_engine.begin() as conn:
        health.register_collector(conn, name, enabled=True)
    runner = CollectorRunner(db_engine, load_schedule())
    asyncio.run(runner.run_once(StubCollector(name, outcome)))

    assert job_statuses(db_engine, name) == [expected_status]
    row = health_row(db_engine, name)
    assert row["records_added_last"] == expected_added
    if expected_status == "ok":
        assert row["consecutive_failures"] == 0 and row["last_success_at"] is not None
    else:
        assert row["consecutive_failures"] == 1 and row["last_error"]


def test_close_interrupted_runs_can_be_scoped(db_engine: Engine) -> None:
    from desk.collectors.runner import close_interrupted_runs
    from desk.metrics import record_job_start

    mine, theirs = f"mine_{uuid4().hex[:6]}", f"theirs_{uuid4().hex[:6]}"
    with db_engine.begin() as conn:
        record_job_start(conn, job=mine, desk="data")
        record_job_start(conn, job=theirs, desk="data")
    close_interrupted_runs(db_engine, jobs={mine})
    assert job_statuses(db_engine, mine) == ["failed"]
    assert job_statuses(db_engine, theirs) == ["running"]
    close_interrupted_runs(db_engine)
    assert job_statuses(db_engine, theirs) == ["failed"]


# --- Gap detection ------------------------------------------------------------------------

NY = ZoneInfo("America/New_York")


def test_find_gaps_always_on() -> None:
    schedule = CollectorSchedule(interval_seconds=120)
    start = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
    runs = [start + timedelta(minutes=m) for m in (1, 3, 5, 20, 22)]
    gaps = find_gaps(runs, start, start + timedelta(minutes=23), schedule, NY)
    assert [(g.start.minute, g.end.minute) for g in gaps] == [(5, 20)]


def test_find_gaps_ignores_closed_window() -> None:
    window = Window(days=("mon", "tue", "wed", "thu", "fri"), start=time(6), end=time(20))
    schedule = CollectorSchedule(interval_seconds=90, window=window)
    # Last run 19:59 ET Tuesday, next run 06:01 ET Wednesday: no gap while closed.
    last = datetime(2026, 9, 22, 19, 59, tzinfo=NY).astimezone(UTC)
    first = datetime(2026, 9, 23, 6, 1, tzinfo=NY).astimezone(UTC)
    gaps = find_gaps([last, first], last, first, schedule, NY)
    assert gaps == []
