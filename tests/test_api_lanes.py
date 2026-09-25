from datetime import date
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import Engine

from desk.api.app import create_app
from desk.artifacts.idea import DroppedCandidate, IdeaSelection, LaneCandidate
from desk.artifacts.raw_record import RawRecord, content_hash
from desk.artifacts.research import Claim, VerifiedClaim
from desk.artifacts.store import append_artifact
from desk.artifacts.thesis import Thesis


def candidate(instrument: str, score: float) -> LaneCandidate:
    return LaneCandidate(
        produced_by="idea.lanes.screen",
        runtime_ms=0,
        lane="screen",
        instrument=instrument,
        driver=f"{instrument} broke out",
        score=score,
        ingredients=[{"name": "importance", "value": score, "source_ref": "t"}],
    )


def test_lanes_board_places_candidates_by_stage(db_engine: Engine) -> None:
    written, shortlisted, cut = (
        candidate("TSTL1", 0.9),
        candidate("TSTL2", 0.8),
        candidate("TSTL3", 0.3),
    )
    selection = IdeaSelection(
        produced_by="idea.select",
        runtime_ms=0,
        parents=(written.id, shortlisted.id, cut.id),
        selected=(written.id,),
        dropped=(
            DroppedCandidate(
                candidate_id=shortlisted.id, reason="no verified evidence from research"
            ),
            DroppedCandidate(candidate_id=cut.id, reason="below the research cut"),
        ),
    )
    record = RawRecord(
        produced_by="data.test",
        runtime_ms=0,
        source="finnhub.company_news",
        source_id=str(uuid4()),
        fetched_at=selection.created_at,
        payload={"headline": "TSTL1"},
        content_hash=content_hash("finnhub.company_news", str(uuid4())),
    )
    claim = Claim(
        produced_by="research",
        runtime_ms=0,
        parents=(record.id,),
        subject="TSTL1",
        statement="TSTL1 grew.",
        source_record_id=record.id,
        quoted_span="TSTL1",
    )
    verified = VerifiedClaim(
        produced_by="factcheck",
        runtime_ms=0,
        parents=(claim.id,),
        claim_id=claim.id,
        verdict="verified",
        reason="ok",
    )
    condition = {"instrument": "TSTL1", "operator": "below", "level_ref": "x"}
    thesis = Thesis.model_validate(
        {
            "produced_by": "idea.writer",
            "runtime_ms": 0,
            "parents": [written.id, verified.id],
            "statement": "TSTL1 rises.",
            "origin": "screen",
            "instruments": ["TSTL1"],
            "direction": "long",
            "horizon": "weeks",
            "review_by": date(2026, 11, 1),
            "drivers": [{"statement": "Orders", "metric": "orders", "source": "edgar"}],
            "evidence": [verified.id],
            "invalidation": {
                "warning": {**condition, "measure": "intraday_price", "level": "95"},
                "hard": {**condition, "measure": "daily_close", "level": "90"},
            },
            "conviction": 3,
            "state": "active",
        }
    )
    with db_engine.begin() as conn:
        for artifact in (written, shortlisted, cut, selection, record, claim, verified, thesis):
            append_artifact(conn, artifact)

    api = TestClient(create_app(db_engine, ollama_base_url="http://127.0.0.1:9"))
    board = api.get(f"/api/lanes?selection={selection.id}").json()
    screen = next(lane for lane in board["lanes"] if lane["lane"] == "screen")
    stages = {card["instrument"]: card["stage"] for card in screen["cards"]}
    assert stages == {"TSTL1": "thesis", "TSTL2": "promoted", "TSTL3": "candidate"}
    assert screen["stage_counts"] == {"thesis": 1, "promoted": 1, "candidate": 1}
    assert api.get(f"/api/lanes?selection={uuid4()}").status_code == 404
