from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import Engine

from desk.api.app import create_app
from desk.api.health import evidence_score
from desk.artifacts.raw_record import RawRecord, content_hash
from desk.artifacts.research import Claim, Dossier, DossierSection, VerifiedClaim
from desk.artifacts.store import append_artifact


def test_evidence_score_counts_unaccepted_claims_as_zero() -> None:
    assert evidence_score([], 0) is None
    assert evidence_score([0.9, 0.6], 2) == 0.75
    assert evidence_score([0.9], 3) == 0.3


def test_research_desk_detail_and_dossier_lineage(db_engine: Engine) -> None:
    record = RawRecord(
        produced_by="data.test",
        runtime_ms=0,
        source="finnhub.company_news",
        source_id=str(uuid4()),
        url="https://example.com/story",
        fetched_at=datetime.now(UTC),
        tickers=("TSTDOS",),
        payload={"headline": "Test Dossier Corp", "summary": "Orders rose to a record."},
        content_hash=content_hash("finnhub.company_news", str(uuid4())),
    )
    claim = Claim(
        produced_by="research",
        runtime_ms=0,
        parents=(record.id,),
        subject="TSTDOS",
        statement="Orders reached a record.",
        source_record_id=record.id,
        quoted_span="Orders rose to a record",
    )
    verified = VerifiedClaim(
        produced_by="factcheck",
        runtime_ms=0,
        parents=(claim.id,),
        claim_id=claim.id,
        verdict="verified",
        entailment=0.9,
        reason="quote found in source; support strong",
    )
    dossier = Dossier(
        produced_by="research",
        runtime_ms=0,
        parents=(claim.id,),
        subject="TSTDOS",
        subject_kind="play",
        selection_score=0.8,
        sections=(
            DossierSection(title="Why selected", text="TSTDOS broke above its range"),
            DossierSection(title="What happened", text="Orders hit a record [c1]."),
        ),
        claim_ids=(claim.id,),
    )
    with db_engine.begin() as conn:
        for artifact in (record, claim, verified, dossier):
            append_artifact(conn, artifact)

    client = TestClient(create_app(db_engine, ollama_base_url="http://127.0.0.1:9"))
    lineage = client.get(f"/api/lineage/{dossier.id}").json()
    kinds = {entry["artifact"]["kind"] for entry in lineage}
    assert {"dossier", "claim", "raw_record"} <= kinds
    assert any(e["artifact"].get("url") == "https://example.com/story" for e in lineage)

    rows = client.get("/api/desks/research").json()["dossiers"]
    row = next(r for r in rows if r["subject"] == "TSTDOS")
    assert (row["claims"], row["verified"], row["pending"], row["evidence"]) == (1, 1, 0, 0.9)
    assert row["why"] == "TSTDOS broke above its range"
