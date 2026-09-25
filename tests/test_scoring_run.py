from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from desk.api.app import create_app
from desk.artifacts.analyst import AnalystView, DebateVerdict, HoldingRating, Point, Rubric
from desk.artifacts.raw_record import RawRecord, content_hash
from desk.artifacts.research import Claim, VerifiedClaim
from desk.artifacts.scoring import Fill, Position, Score
from desk.artifacts.store import append_artifact, get_artifact
from desk.artifacts.thesis import Thesis
from desk.artifacts.trade import CheckRecord, Leg, RiskDecision, TradePlan
from desk.scoring.report import rollups
from desk.scoring.run import run_scoring

NY = ZoneInfo("America/New_York")
SYMBOL = "TSTSC"


def seed(engine: Engine, now: datetime) -> dict[str, object]:
    start = now - timedelta(days=30)
    today = now.astimezone(NY).date()
    record = RawRecord(
        produced_by="data.test",
        runtime_ms=0,
        source="finnhub.company_news",
        source_id=str(uuid4()),
        fetched_at=start,
        tickers=(SYMBOL,),
        payload={"headline": SYMBOL, "summary": "TSTSC orders rose."},
        content_hash=content_hash("finnhub.company_news", str(uuid4())),
        created_at=start,
    )
    claim = Claim(
        produced_by="research",
        runtime_ms=0,
        parents=(record.id,),
        subject=SYMBOL,
        statement="TSTSC orders rose.",
        source_record_id=record.id,
        quoted_span="TSTSC orders rose",
        created_at=start,
    )
    verified = VerifiedClaim(
        produced_by="factcheck",
        runtime_ms=0,
        parents=(claim.id,),
        claim_id=claim.id,
        verdict="verified",
        reason="ok",
        created_at=start,
    )
    condition = {"instrument": SYMBOL, "operator": "below", "level_ref": f"levels:{SYMBOL}:x"}
    thesis = Thesis.model_validate(
        {
            "produced_by": "idea.writer",
            "runtime_ms": 0,
            "parents": [verified.id],
            "statement": "TSTSC rises.",
            "origin": "screen",
            "instruments": [SYMBOL],
            "direction": "long",
            "horizon": "weeks",
            "review_by": today + timedelta(days=5),
            "drivers": [{"statement": "Orders", "metric": "orders", "source": "edgar"}],
            "evidence": [verified.id],
            "invalidation": {
                "warning": {**condition, "measure": "intraday_price", "level": "95"},
                "hard": {**condition, "measure": "daily_close", "level": "90"},
            },
            "conviction": 3,
            "state": "active",
            "created_at": start,
        }
    )
    point = Point(text="Orders rose.", claim_ids=(verified.id,))
    views = [
        AnalystView(
            produced_by=f"analyst.{persona}",
            runtime_ms=0,
            parents=(thesis.id, verified.id),
            thesis_id=thesis.id,
            subject=SYMBOL,
            persona=persona,
            role="view",
            stance=stance,
            points=(point,),
            confidence_label="medium",
            created_at=start,
        )
        for persona, stance in (("technician", "for"), ("macro", "against"))
    ]
    verdict = DebateVerdict(
        produced_by="analyst.judge",
        runtime_ms=0,
        parents=(thesis.id, *(v.id for v in views), verified.id),
        thesis_id=thesis.id,
        subject=SYMBOL,
        view_ids=tuple(v.id for v in views),
        bull=(point,),
        bear=(point,),
        rebuttal=(point,),
        rubric=Rubric(evidence_quality="strong", rebuttal="adequate", risk_reward="good"),
        verdict="pursue",
        confidence_label="high",
        reasons=(point,),
        dissent="Slowdown.",
        created_at=start,
    )
    keep = AnalystView(
        produced_by="holdings.technician",
        runtime_ms=0,
        parents=(),
        subject=SYMBOL,
        persona="technician",
        role="keep",
        stance="for",
        points=(Point(text="Trend up.", fact_refs=(f"levels:{SYMBOL}:x",)),),
        confidence_label="medium",
        created_at=start,
    )
    rating = HoldingRating(
        produced_by="holdings.judge",
        runtime_ms=0,
        parents=(keep.id,),
        subject=SYMBOL,
        account_refs=("ibkr:0055",),
        rating="buy_add",
        confidence_label="medium",
        reasons=(Point(text="Trend up.", fact_refs=(f"levels:{SYMBOL}:x",)),),
        what_would_change_it="A break lower.",
        suggested_action="Add.",
        created_at=start,
    )
    leg = Leg(action="buy", kind="shares", symbol=SYMBOL, price=Decimal(100), price_ref="bars")
    plans, decisions = [], []
    for outcome in ("approved", "vetoed"):
        plan = TradePlan(
            produced_by="trader",
            runtime_ms=0,
            parents=(thesis.id, verdict.id),
            thesis_id=thesis.id,
            account_ref="ibkr:0055",
            subject=SYMBOL,
            instrument=SYMBOL,
            structure="shares",
            direction="long",
            legs=(leg,),
            entry=Decimal(100),
            entry_ref="bars",
            stop=Decimal(90),
            stop_ref="x",
            target=Decimal(120),
            target_ref="y",
            unit_cost=Decimal(100),
            unit_max_loss=Decimal(10),
            conviction=3,
            rationale="Shares.",
            created_at=start,
        )
        failed = outcome == "vetoed"
        decision = RiskDecision(
            produced_by="risk",
            runtime_ms=0,
            parents=(plan.id,),
            plan_id=plan.id,
            decision=outcome,
            tier_pct=Decimal(5),
            cap=Decimal(125),
            requested_size=12,
            size=0 if failed else 12,
            max_loss=Decimal(0 if failed else 120),
            cost=Decimal(0 if failed else 1200),
            funding_needed=Decimal(0),
            checks=(
                CheckRecord(name="liquidity", result="fail" if failed else "pass", detail="d"),
            ),
            veto_reasons=("wide spread",) if failed else (),
            created_at=start,
        )
        plans.append(plan)
        decisions.append(decision)
    fills = [
        Fill(
            produced_by="data.ibkr_flex",
            runtime_ms=0,
            broker="ibkr",
            account_ref="ibkr:0055",
            exec_id=f"tstsc-{side}",
            symbol=SYMBOL,
            contract=SYMBOL,
            asset_class="equity",
            side=side,
            quantity=Decimal(10),
            price=Decimal(price),
            multiplier=Decimal(1),
            fees=Decimal(1),
            executed_at=when,
        )
        for side, price, when in (
            ("buy", "100", start + timedelta(days=1)),
            ("sell", "112", start + timedelta(days=12)),
        )
    ]
    with engine.begin() as conn:
        for artifact in (
            record,
            claim,
            verified,
            thesis,
            *views,
            verdict,
            keep,
            rating,
            *plans,
            *decisions,
            *fills,
        ):
            append_artifact(conn, artifact)
        # A steady climb from 100: the thesis, the verdict and the "for" view are right.
        for offset in range(40, 0, -1):
            day = today - timedelta(days=offset)
            close = Decimal(100) + Decimal(max(0, 30 - offset))
            conn.execute(
                text(
                    "INSERT INTO price_bars (source, symbol, interval, ts, open, high, low, close, "
                    "fetched_at) VALUES ('yahoo', :s, '1d', :ts, :c, :h, :l, :c, now())"
                ),
                {
                    "s": SYMBOL,
                    "ts": datetime.combine(day, time(0), NY),
                    "c": close,
                    "h": close + 1,
                    "l": close - 1,
                },
            )
    return {"thesis": thesis, "views": views, "verdict": verdict, "rating": rating, "plans": plans}


def scores_for(engine: Engine, subject_id: object) -> dict[str, Score]:
    with engine.connect() as conn:
        ids = conn.execute(
            text("SELECT id FROM artifacts WHERE kind = 'score' AND payload->>'subject_id' = :s"),
            {"s": str(subject_id)},
        ).scalars()
        found = [get_artifact(conn, i) for i in ids]
    return {s.horizon: s for s in found if isinstance(s, Score)}


def test_scoring_run_end_to_end(db_engine: Engine) -> None:
    now = datetime.now(UTC)
    seeded = seed(db_engine, now)
    first = run_scoring(db_engine, NY, now)
    assert first.scores > 0 and first.positions >= 1

    thesis = seeded["thesis"]
    thesis_scores = scores_for(db_engine, thesis.id)  # type: ignore[attr-defined]
    assert set(thesis_scores) == {"1d", "5d", "20d"}
    assert thesis_scores["20d"].return_pct > 0 and thesis_scores["20d"].hit is True
    assert thesis_scores["5d"].attribution["lane"] == "screen"

    technician, macro = seeded["views"]  # type: ignore[misc]
    assert scores_for(db_engine, technician.id)["5d"].hit is True
    assert scores_for(db_engine, macro.id)["5d"].hit is False
    assert scores_for(db_engine, macro.id)["5d"].attribution["persona"] == "macro"
    assert scores_for(db_engine, seeded["verdict"].id)["5d"].hit is True  # type: ignore[attr-defined]
    rating = scores_for(db_engine, seeded["rating"].id)["5d"]  # type: ignore[attr-defined]
    assert rating.hit is True and rating.attribution["persona"] == "technician"
    approved, vetoed = seeded["plans"]  # type: ignore[misc]
    vetoed_score = scores_for(db_engine, vetoed.id)["20d"]
    assert vetoed_score.attribution["veto_reason"] == "liquidity"
    assert vetoed_score.r_multiple is not None and vetoed_score.entry_price == Decimal(100)

    with db_engine.connect() as conn:
        position_id = conn.execute(
            text(
                "SELECT id FROM artifacts WHERE kind = 'position' AND payload->>'contract' = :c "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"c": SYMBOL},
        ).scalar_one()
        position = get_artifact(conn, position_id)
        report = rollups(conn, "5d")
    assert isinstance(position, Position)
    assert position.state == "closed" and position.link_status == "auto"
    assert position.plan_id == approved.id and position.realized_pnl == Decimal(118)
    exit_score = scores_for(db_engine, position.id)["exit"]
    assert exit_score.shadow is False and exit_score.r_multiple == pytest.approx(
        118 / 120, abs=1e-3
    )
    assert exit_score.adherence["size_within_plan"] is True

    lanes = {g["value"]: g for g in report["dimensions"]["lane"]}
    personas = {g["value"]: g for g in report["dimensions"]["persona"]}
    assert lanes["screen"]["count"] >= 1 and {"technician", "macro"} <= set(personas)

    again = run_scoring(db_engine, NY, now)
    assert again.positions == 0
    assert scores_for(db_engine, thesis.id).keys() == thesis_scores.keys()  # type: ignore[attr-defined]

    client = TestClient(create_app(db_engine, ollama_base_url="http://127.0.0.1:9"))
    scored = client.get("/api/scores?horizon=5d").json()
    assert scored["dimensions"]["persona"] and scored["recent"]
    linked = client.post(
        f"/api/positions/{position.id}/link",
        json={"thesis_id": str(thesis.id)},  # type: ignore[attr-defined]
    )
    assert linked.status_code == 200 and linked.json()["link_status"] == "confirmed"
