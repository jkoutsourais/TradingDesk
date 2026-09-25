from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import Engine

from desk.api.app import create_app
from desk.collectors import health
from desk.collectors.base import AccountSnapshot, CollectResult, PositionRow
from desk.collectors.ingest import ingest


def test_desks_and_book_endpoints(db_engine: Engine) -> None:
    with db_engine.begin() as conn:
        health.register_collector(conn, "fed_speeches", enabled=True)
        health.record_success(conn, "fed_speeches", 3)
        health.register_collector(conn, "fred", enabled=False, disabled_reason="no key")
        ingest(
            conn,
            CollectResult(
                accounts=[
                    AccountSnapshot(
                        source="ibkr_flex",
                        broker="ibkr",
                        account_ref="ibkr:0000",
                        as_of=datetime(2026, 9, 21, 20, tzinfo=UTC),
                        fetched_at=datetime.now(UTC),
                        net_liquidation=Decimal(1000),
                        cash=Decimal(100),
                        settled_cash=Decimal(100),
                        buying_power=None,
                        currency="USD",
                        positions=(
                            PositionRow(
                                "DEVX",
                                "DEVX INC",
                                "equity",
                                Decimal(3),
                                Decimal(1),
                                None,
                                Decimal(30),
                                Decimal(12),
                                Decimal(36),
                                "USD",
                            ),
                        ),
                    )
                ]
            ),
        )

    client = TestClient(create_app(db_engine, ollama_base_url="http://127.0.0.1:9"))
    desks = client.get("/api/desks").json()
    states = {c["collector"]: c["state"] for c in desks["collectors"]}
    assert states["fed_speeches"] == "ok"
    assert states["fred"] == "disabled"
    refs = {h["account_ref"] for h in client.get("/api/book").json()["holdings"]}
    assert "ibkr:0000" in refs
    assert "raw_records_total" in client.get("/api/desks/data").json()["volumes"]
    watch = client.get("/api/desks/watch").json()
    assert {"triggers", "shifts", "calendar"} <= set(watch)
    assert client.get("/api/desks/risk").json() == {}
