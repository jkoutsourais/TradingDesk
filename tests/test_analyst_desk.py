import asyncio
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from desk.api.app import create_app
from desk.artifacts.analyst import DebateVerdict, HoldingRating, RiskFlag
from desk.artifacts.raw_record import RawRecord, content_hash
from desk.artifacts.research import Claim, VerifiedClaim
from desk.artifacts.store import append_artifact, get_artifact
from desk.artifacts.thesis import Thesis
from desk.collectors.base import AccountSnapshot, CollectResult, PositionRow
from desk.collectors.ingest import ingest
from desk.config import load_models, load_tiers, load_universe
from desk.desks.analyst.debate import (
    DraftPoint,
    JudgeDraft,
    check_judge,
    point_problems,
    to_points,
)
from desk.desks.analyst.facts import FactBook
from desk.desks.analyst.personas import load_personas
from desk.desks.analyst.run import run_analyst_desks
from desk.desks.holdings.flags import (
    Holding,
    load_holdings_config,
    option_expiry,
    risk_flags,
)
from desk.desks.holdings.rating import apply_flags
from desk.desks.risk.config import load_risk_config
from desk.llm.client import StructuredResult, Usage
from desk.llm.facts import Fact
from desk.watch.calendar import MarketCalendar, load_calendar_config

HOLDINGS = load_holdings_config()
LEVERAGE = load_risk_config().levered_funds
TODAY = date(2026, 9, 24)


# --- Units --------------------------------------------------------------------------------


def test_option_expiry_formats() -> None:
    assert option_expiry("SLV   261016C00030000") == date(2026, 10, 16)
    assert option_expiry("SLV 16OCT26 30 C") == date(2026, 10, 16)
    assert option_expiry("AMERICAN ELECTRIC POWER") is None


def test_risk_flags_and_forcing() -> None:
    big = Holding("AEP", ["ibkr:0000"], {"equity"}, max_share_pct=40.0)
    flags = risk_flags(big, HOLDINGS, LEVERAGE, TODAY)
    assert flags[0].code == "concentration" and flags[0].forces == "trim"
    levered = Holding("TQQQ", ["ibkr:0000"], {"equity"}, first_seen=TODAY - timedelta(days=6))
    assert risk_flags(levered, HOLDINGS, LEVERAGE, TODAY)[0].forces is None
    old = Holding("TQQQ", ["ibkr:0000"], {"equity"}, first_seen=TODAY - timedelta(days=20))
    assert risk_flags(old, HOLDINGS, LEVERAGE, TODAY)[0].forces == "trim"
    expiring = Holding("SLV", ["ibkr:0000"], {"option"}, expiries=[TODAY + timedelta(days=2)])
    assert risk_flags(expiring, HOLDINGS, LEVERAGE, TODAY)[0].forces == "sell"
    assert risk_flags(Holding("AEP", ["ibkr:0000"], {"equity"}), HOLDINGS, LEVERAGE, TODAY) == []


def test_apply_flags_only_raises_severity() -> None:
    trim = RiskFlag(code="concentration", detail="big", forces="trim")
    assert apply_flags("buy_add", [trim]) == ("trim", "buy_add")
    assert apply_flags("sell", [trim]) == ("sell", None)
    assert apply_flags("hold", []) == ("hold", None)


def book() -> FactBook:
    made = FactBook()
    claim_id = uuid4()
    made.claims["claim_1"] = claim_id
    made.add(Fact("claim_1", "s", "", "Orders rose.", "verified claim on X", f"vc:{claim_id}"))
    made.add(Fact("lvl_1", Decimal(28), "USD", "$28.00", "X 20-day low", "levels:X:low_20d"))
    return made


def test_point_checks_and_rendering() -> None:
    facts = book()
    good = DraftPoint(text="Support sits at {lvl_1}.", cites=["lvl_1", "claim_1"])
    assert point_problems(facts, [good], "p") == []
    bad = DraftPoint(text="Support at 27.", cites=["lvl_9"])
    problems = point_problems(facts, [bad], "p")
    assert any("unknown fact ids" in p for p in problems) and any("'27'" in p for p in problems)
    rendered = to_points(facts, [good])[0]
    assert rendered.text == "Support sits at $28.00."
    assert rendered.claim_ids == (facts.claims["claim_1"],)
    assert rendered.fact_refs == ("levels:X:low_20d",)


def test_judge_must_cite_a_claim() -> None:
    reply = JudgeDraft(
        evidence_quality="adequate",
        rebuttal="weak",
        risk_reward="fair",
        verdict="watch",
        confidence="medium",
        conviction="low",
        reasons=[DraftPoint(text="Levels only.", cites=["lvl_1"])],
        dissent="None.",
    )
    assert any("claim_N" in p for p in check_judge(book())(reply))


# --- End to end ---------------------------------------------------------------------------


class ScriptedChat:
    """Cites the first listed fact (a claim when one exists) and answers every schema."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.unloaded: list[str] = []

    async def unload(self, model: str) -> None:
        self.unloaded.append(model)

    async def structured(self, model: Any, prompt: Any, schema: Any, check: Any) -> Any:
        self.calls.append(schema.__name__)
        ids = re.findall(r"^\{([^}]+)\} = ", prompt.user, re.MULTILINE)
        claims = [i for i in ids if i.startswith("claim_")]
        cite = (claims or ids)[0]
        point = {"text": "The evidence supports this.", "cites": [cite]}
        replies = {
            "ViewDraft": {"stance": "for", "points": [point], "confidence": "medium"},
            "ArgumentDraft": {"points": [point]},
            "JudgeDraft": {
                "evidence_quality": "strong",
                "rebuttal": "adequate",
                "risk_reward": "good",
                "verdict": "pursue",
                "confidence": "high",
                "conviction": "high",
                "reasons": [point],
                "dissent": "The bear doubts the orders repeat.",
            },
            "RatingDraft": {
                "rating": "buy_add",
                "confidence": "low",
                "reasons": [point],
                "what_would_change_it": "A close below the recent low.",
                "suggested_action": "Add slowly.",
            },
        }
        value = schema.model_validate(replies[schema.__name__])
        assert check(value) == []
        return StructuredResult(value, Usage(model=model.model), 1)


def seed(engine: Engine, now: datetime) -> Thesis:
    record = RawRecord(
        produced_by="data.test",
        runtime_ms=0,
        source="finnhub.company_news",
        source_id=str(uuid4()),
        fetched_at=now,
        tickers=("TSTDB",),
        payload={"headline": "TSTDB order", "summary": "TSTDB won a large order."},
        content_hash=content_hash("finnhub.company_news", str(uuid4())),
    )
    claim = Claim(
        produced_by="research",
        runtime_ms=0,
        parents=(record.id,),
        subject="TSTDB",
        statement="TSTDB won a large order.",
        source_record_id=record.id,
        quoted_span="TSTDB won a large order",
    )
    verified = VerifiedClaim(
        produced_by="factcheck",
        runtime_ms=0,
        parents=(claim.id,),
        claim_id=claim.id,
        verdict="verified",
        entailment=0.9,
        reason="quote found",
    )
    condition = {"instrument": "TSTDB", "operator": "below", "level_ref": "levels:TSTDB:x"}
    thesis = Thesis.model_validate(
        {
            "produced_by": "idea.writer",
            "runtime_ms": 0,
            "parents": [verified.id],
            "statement": "Orders drive TSTDB higher.",
            "origin": "screen",
            "instruments": ["TSTDB"],
            "direction": "long",
            "horizon": "weeks",
            "review_by": date(2026, 11, 1),
            "drivers": [{"statement": "Orders continue", "metric": "orders", "source": "edgar"}],
            "evidence": [verified.id],
            "invalidation": {
                "warning": {**condition, "measure": "intraday_price", "level": "95"},
                "hard": {**condition, "measure": "daily_close", "level": "90"},
            },
            "conviction": 2,
            "state": "active",
        }
    )
    position = PositionRow(
        "TSTHL",
        "TSTHL INC",
        "equity",
        Decimal(5),
        Decimal(1),
        None,
        Decimal(400),
        Decimal(100),
        Decimal(500),
        "USD",
    )
    with engine.begin() as conn:
        for artifact in (record, claim, verified, thesis):
            append_artifact(conn, artifact)
        ingest(
            conn,
            CollectResult(
                accounts=[
                    AccountSnapshot(
                        source="ibkr_flex",
                        broker="ibkr",
                        account_ref="ibkr:0099",
                        as_of=now - timedelta(hours=12),
                        fetched_at=now,
                        net_liquidation=Decimal(1000),
                        cash=Decimal(500),
                        settled_cash=Decimal(500),
                        buying_power=None,
                        currency="USD",
                        positions=(position,),
                    )
                ]
            ),
        )
    return thesis


def test_pre_market_rates_holdings_and_debates_theses(db_engine: Engine) -> None:
    now = datetime.now(UTC)
    thesis = seed(db_engine, now)
    chat = ScriptedChat()
    models = load_models()
    outcome = asyncio.run(
        run_analyst_desks(
            db_engine,
            chat,  # type: ignore[arg-type]
            models.deep,
            load_personas(),
            load_tiers(),
            load_universe(),
            HOLDINGS,
            LEVERAGE,
            MarketCalendar(load_calendar_config()),
            now,
            now + timedelta(hours=1),
        )
    )
    assert outcome.errors == [], outcome.errors
    assert {"RatingDraft", "ViewDraft", "JudgeDraft"} <= set(chat.calls)

    with db_engine.connect() as conn:
        rating_id = conn.execute(
            text(
                "SELECT id FROM artifacts WHERE kind = 'holding_rating' "
                "AND payload->>'subject' = 'TSTHL' ORDER BY created_at DESC LIMIT 1"
            )
        ).scalar_one()
        rating = get_artifact(conn, rating_id)
        verdict_id = conn.execute(
            text(
                "SELECT id FROM artifacts WHERE kind = 'debate_verdict' "
                "AND payload->>'thesis_id' = :t"
            ),
            {"t": str(thesis.id)},
        ).scalar_one()
        verdict = get_artifact(conn, verdict_id)
        updated_id = conn.execute(
            text("SELECT id FROM artifacts WHERE kind = 'thesis' AND payload->>'previous_id' = :t"),
            {"t": str(thesis.id)},
        ).scalar_one()
        updated = get_artifact(conn, updated_id)
    # 50% of the account forces a trim over the judge's buy/add.
    assert isinstance(rating, HoldingRating)
    assert rating.rating == "trim" and rating.judged_rating == "buy_add"
    assert rating.suggested_action.startswith("Trim forced by risk rule")
    assert isinstance(verdict, DebateVerdict) and verdict.verdict == "pursue"
    assert verdict.view_ids and verdict.reasons[0].claim_ids
    assert isinstance(updated, Thesis) and updated.conviction == 4
    assert verdict_id in updated.parents

    client = TestClient(create_app(db_engine, ollama_base_url="http://127.0.0.1:9"))
    page = client.get(f"/debates/{verdict_id}")
    assert page.status_code == 200 and "TSTDB won a large order." in page.text
    analyst = client.get("/dev/status").json()["analyst"]
    assert any(r["subject"] == "TSTHL" and r["judged"] == "buy_add" for r in analyst["ratings"])

    # Debated and unchanged: not due again at the next shift.
    again = asyncio.run(
        run_analyst_desks(
            db_engine,
            ScriptedChat(),  # type: ignore[arg-type]
            models.deep,
            load_personas(),
            load_tiers(),
            load_universe(),
            HOLDINGS,
            LEVERAGE,
            MarketCalendar(load_calendar_config()),
            now,
            now + timedelta(hours=1),
        )
    )
    assert not any(note.startswith("debate TSTDB") for note in again.notes)
