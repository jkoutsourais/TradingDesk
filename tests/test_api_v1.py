from fastapi.testclient import TestClient
from sqlalchemy import Engine

from desk.api.app import create_app
from desk.artifacts.raw_record import RawRecord, content_hash
from desk.artifacts.store import append_artifact
from desk.artifacts.trigger import Trigger


def client(engine: Engine) -> TestClient:
    return TestClient(create_app(engine, ollama_base_url="http://127.0.0.1:9"))


def test_status_today_and_desks(db_engine: Engine) -> None:
    api = client(db_engine)
    status = api.get("/api/status").json()
    assert set(status) >= {"shift", "models", "collectors", "last_job"}
    assert status["models"] is None  # Ollama unreachable in tests
    today = api.get("/api/today").json()
    assert set(today) == {"brief", "theses", "plans", "vetoes", "ratings"}
    desks = api.get("/api/desks").json()
    assert [d["id"] for d in desks["desks"]][:3] == ["data", "watch", "research"]


def test_lineage_walks_parents(db_engine: Engine) -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    record = RawRecord(
        produced_by="data.test",
        runtime_ms=0,
        source="finnhub.company_news",
        source_id=str(uuid4()),
        fetched_at=datetime.now(UTC),
        payload={"headline": "Lineage test"},
        content_hash=content_hash("finnhub.company_news", str(uuid4())),
    )
    trigger = Trigger(
        produced_by="watch.test",
        runtime_ms=0,
        parents=(record.id,),
        rule_id="filing",
        instrument="TSTLN",
        importance=0.5,
        urgent=False,
        summary="TSTLN filing",
        fingerprint=f"test:{uuid4()}",
    )
    with db_engine.begin() as conn:
        append_artifact(conn, record)
        append_artifact(conn, trigger)
    entries = client(db_engine).get(f"/api/lineage/{trigger.id}").json()
    assert [e["depth"] for e in entries] == [0, 1]
    assert entries[1]["artifact"]["payload"]["headline"] == "Lineage test"
    assert client(db_engine).get(f"/api/lineage/{uuid4()}").status_code == 404
