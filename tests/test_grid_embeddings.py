import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pandas as pd
import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from desk.artifacts.raw_record import RawRecord, content_hash
from desk.artifacts.store import append_artifact
from desk.collectors.base import CollectResult, GridObservation
from desk.collectors.embeddings import NewsEmbeddings, record_text
from desk.collectors.grid import collect_iso, fetch_dates, frame_to_observations
from desk.collectors.ingest import ingest
from desk.collectors.purge import purge_raw_records
from desk.config import GridIso, GridPrices, load_models
from desk.llm.embeddings import EmbeddingError, OllamaEmbedder

NOW = datetime(2026, 9, 23, 2, 0, tzinfo=UTC)  # 21:00 CDT on Sep 22
CT = "US/Central"


# --- Grid -------------------------------------------------------------------------------


def frame(starts: list[str], values: dict[str, list[float]], with_end: bool = True) -> pd.DataFrame:
    index = pd.to_datetime(starts).tz_localize(CT)
    data: dict[str, Any] = {"Interval Start": index}
    if with_end:
        data["Interval End"] = index + pd.Timedelta(minutes=5)
    data.update(values)
    return pd.DataFrame(data)


def test_frame_to_observations_keeps_only_settled_intervals() -> None:
    df = frame(
        ["2026-09-22 20:30", "2026-09-22 20:35", "2026-09-22 20:40", "2026-09-22 20:55"],
        {"Load": [50000.0, float("nan"), 50100.0, 50200.0]},
    )
    cutoff = NOW - timedelta(minutes=15)  # 20:45 CDT
    rows = frame_to_observations("ercot", df, {"Load": "load"}, "MW", cutoff, NOW)
    assert [(r.interval_start.astimezone(UTC).strftime("%H:%M"), r.value) for r in rows] == [
        ("01:30", Decimal("50000.0000")),
        ("01:40", Decimal("50100.0000")),
    ]
    assert all(r.interval_minutes == 5 for r in rows)


def test_pandas_na_values_are_skipped() -> None:
    df = frame(["2026-09-22 20:00", "2026-09-22 20:05"], {"Other": [1.0, 2.0]})
    df["Other"] = pd.array([pd.NA, 7], dtype="Int64")
    rows = frame_to_observations("miso", df, {"Other": "fuel.other"}, "MW", NOW, NOW)
    assert [r.value for r in rows] == [Decimal("7.0000")]


def test_interval_length_inferred_without_end_column() -> None:
    df = frame(
        ["2026-09-22 19:00", "2026-09-22 19:15", "2026-09-22 19:30"],
        {"Solar": [1.0, 2.0, 3.0]},
        with_end=False,
    )
    rows = frame_to_observations("ercot", df, {"Solar": "fuel.solar"}, "MW", NOW, NOW)
    assert {r.interval_minutes for r in rows} == {15}


class FakeIso:
    default_timezone = CT

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def get_load(self, day: Any) -> pd.DataFrame:
        self.calls.append(("load", day))
        return frame(["2026-09-22 20:00"], {"Load": [51000.0]})

    def get_fuel_mix(self, day: Any) -> pd.DataFrame:
        raise RuntimeError("upstream 503")

    def get_spp(self, day: Any, market: str, location_type: str) -> pd.DataFrame:
        df = frame(["2026-09-22 20:00", "2026-09-22 20:00"], {"SPP": [31.5, 29.0]})
        df["Location"] = ["HB_NORTH", "HB_OTHER"]
        return df


def test_collect_iso_isolates_failures_and_filters_locations() -> None:
    config = GridIso(
        load=True,
        fuel_mix=True,
        prices=GridPrices(
            method="get_spp",
            market="REAL_TIME_15_MIN",
            location_type="Trading Hub",
            locations=("HB_NORTH", "HB_MISSING"),
        ),
    )
    rows, errors = collect_iso("ercot", config, FakeIso(), 15, NOW)
    assert sorted(r.series_id for r in rows) == ["load", "price.hb_north"]
    assert any("fuel_mix" in e and "upstream 503" in e for e in errors)
    assert any("HB_MISSING" in e for e in errors)


def test_fetch_dates_rereads_yesterday_after_local_midnight() -> None:
    iso = FakeIso()
    assert fetch_dates(iso, NOW) == ["today"]
    just_after_midnight = datetime(2026, 9, 23, 5, 30, tzinfo=UTC)  # 00:30 CDT
    assert fetch_dates(iso, just_after_midnight) == ["today", date(2026, 9, 22)]


def test_grid_rows_insert_once_and_are_append_only(db_conn: Connection) -> None:
    obs = GridObservation("test_iso", "load", NOW, 5, Decimal("1.0"), "MW", NOW)
    assert ingest(db_conn, CollectResult(grid=[obs])).grid_added == 1
    assert ingest(db_conn, CollectResult(grid=[obs])).grid_added == 0
    with pytest.raises(DBAPIError, match="append-only"):
        db_conn.execute(text("UPDATE grid_observations SET value = 2"))


# --- Embeddings -------------------------------------------------------------------------


def test_record_text_per_source() -> None:
    assert record_text("finnhub.company_news", {"headline": "H", "summary": "S"}, None) == "H S"
    truth = record_text("truth_social.post", {"content": "<p>Tariffs &amp; steel</p>"}, None)
    assert truth == "Tariffs & steel"
    assert record_text("fed.speech", {}, "https://fed/x") == "https://fed/x"


def embed_transport(width: int, captured: list[dict[str, Any]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        captured.append(body)
        return httpx.Response(200, json={"embeddings": [[0.1] * width for _ in body["input"]]})

    return httpx.MockTransport(handler)


def test_embedder_prefixes_forces_cpu_and_checks_width() -> None:
    config = load_models().embedding
    captured: list[dict[str, Any]] = []
    embedder = OllamaEmbedder(
        "http://ollama.test", config, transport=embed_transport(768, captured)
    )
    vectors = asyncio.run(embedder.embed_documents(["gold rallies"]))
    assert len(vectors[0]) == 768
    assert captured[0]["input"] == ["search_document: gold rallies"]
    assert captured[0]["options"] == {"num_gpu": 0}

    wrong = OllamaEmbedder("http://ollama.test", config, transport=embed_transport(512, []))
    with pytest.raises(EmbeddingError, match="width"):
        asyncio.run(wrong.embed_documents(["x"]))


def news(source: str, headline: str, age_days: float = 0) -> RawRecord:
    stamp = datetime.now(UTC) - timedelta(days=age_days)
    return RawRecord(
        produced_by="data.test",
        runtime_ms=0,
        created_at=stamp,
        source=source,
        source_id=headline,
        fetched_at=stamp,
        payload={"headline": headline},
        content_hash=content_hash(source, headline, stamp.isoformat()),
    )


def test_news_embeddings_skip_filings_and_follow_purge(db_engine: Engine) -> None:
    fresh = news("finnhub.general_news", "fresh story")
    old = news("finnhub.general_news", "old story", age_days=9)
    filing = news("edgar.filing", "form 4")
    with db_engine.begin() as conn:
        for record in (fresh, old, filing):
            append_artifact(conn, record)

    config = load_models().embedding
    embedder = OllamaEmbedder("http://ollama.test", config, transport=embed_transport(768, []))
    collector = NewsEmbeddings(db_engine, embedder, batch_size=64)
    result = asyncio.run(collector.collect())
    embedded = {e.artifact_id for e in result.embeddings}
    assert {fresh.id, old.id} <= embedded
    assert filing.id not in embedded

    with db_engine.begin() as conn:
        ingest(conn, result)
    again = asyncio.run(collector.collect())
    assert not {fresh.id, old.id} & {e.artifact_id for e in again.embeddings}

    with db_engine.begin() as conn:
        purge_raw_records(conn, retention_days=7)
        remaining = (
            conn.execute(
                text("SELECT artifact_id FROM raw_record_embeddings WHERE artifact_id = ANY(:ids)"),
                {"ids": [fresh.id, old.id]},
            )
            .scalars()
            .all()
        )
    assert remaining == [fresh.id]
