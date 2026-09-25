import asyncio
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from desk.api.app import create_app
from desk.artifacts.analyst import DebateVerdict, Point, Rubric
from desk.artifacts.raw_record import RawRecord, content_hash
from desk.artifacts.research import Claim, VerifiedClaim
from desk.artifacts.store import append_artifact, get_artifact
from desk.artifacts.thesis import Thesis
from desk.artifacts.trade import RiskDecision, TradePlan
from desk.collectors.base import AccountSnapshot, CollectResult
from desk.collectors.ingest import ingest
from desk.config import load_models, load_tiers, load_universe
from desk.desks.idea.levels import Level
from desk.desks.risk.config import load_risk_config
from desk.desks.trader.menu import Priced, build_menu, load_trader_config, option_choices
from desk.desks.trader.options import OptionQuote, expiry_window, pick_strikes
from desk.desks.trader.plan import Idea, TraderReply, check_reply, menu_facts
from desk.desks.trader.run import run_trader_desk
from desk.llm.client import StructuredResult, Usage
from desk.llm.facts import FactTable

NEW_YORK = ZoneInfo("America/New_York")
RISK = load_risk_config()
TODAY = date(2026, 9, 24)
EXPIRY = date(2026, 11, 20)


def quote(
    strike: str, bid: str, ask: str, right: Literal["call", "put"] = "call", oi: int = 800
) -> OptionQuote:
    return OptionQuote(
        symbol=f"SLV   261120{'C' if right == 'call' else 'P'}000{int(Decimal(strike) * 1000):05d}",
        underlying="SLV",
        expiry=EXPIRY,
        strike=Decimal(strike),
        right=right,
        bid=Decimal(bid),
        ask=Decimal(ask),
        open_interest=oi,
        delta=0.5,
    )


class FakeOptions:
    def __init__(self, quotes: list[OptionQuote]) -> None:
        self.quotes = quotes
        self.asked: list[tuple[str, str]] = []

    async def candidates(
        self, underlying: str, right: Any, spot: Decimal, horizon_days: int, today: date
    ) -> list[OptionQuote]:
        self.asked.append((underlying, right))
        return [q for q in self.quotes if q.right == right]


def test_option_helpers() -> None:
    earliest, latest = expiry_window(35, TODAY)
    assert earliest == TODAY + timedelta(days=28) and latest == TODAY + timedelta(days=100)
    strikes = [Decimal(s) for s in ("28", "29", "30", "31", "32", "33")]
    assert pick_strikes(strikes, Decimal("30.10"), "call") == [Decimal(30), Decimal(32)]
    assert pick_strikes(strikes, Decimal("30.10"), "put") == [Decimal(29), Decimal(30)]


def test_option_choices_price_premiums_and_spreads() -> None:
    choices = option_choices([quote("30", "1.10", "1.20"), quote("32", "0.40", "0.50")], 1)
    single = choices[0]
    assert single.structure == "long_call" and single.unit_cost == Decimal("115.00")
    assert single.unit_max_loss == single.unit_cost
    spread = choices[-1]
    # Buy the 30 at the ask, sell the 32 at the bid: (1.20 - 0.40) x 100.
    assert spread.structure == "debit_spread" and spread.unit_max_loss == Decimal("80.00")
    assert [leg.action for leg in spread.legs] == ["buy", "sell"]


def test_menu_maps_stops_to_proxies_and_respects_the_account() -> None:
    options = FakeOptions([quote("30", "1.10", "1.20"), quote("30", "0.90", "1.00", "put")])
    subject = Priced("/SI", Decimal("30"), "price_bars:yahoo:/SI:1d:2026-09-23")
    proxies = [
        Priced("SLV", Decimal("28"), "quotes_latest:SLV"),
        Priced("AGQ", Decimal("40"), "quotes_latest:AGQ"),
    ]
    allowed = set(RISK.account_types["roth"].structures)
    choices = asyncio.run(
        build_menu(
            subject,
            "long",
            Decimal("27"),
            proxies,
            set(),
            RISK.levered_funds,
            options,
            35,
            TODAY,
            allowed,
        )
    )
    by_instrument = {c.instrument: c for c in choices}
    # The future itself is not tradeable in the Roth; its ETF and levered fund are.
    assert "/SI" not in by_instrument
    # A 10% stop on the subject: SLV loses 2.80, AGQ (2x) loses 8.00 per share.
    assert by_instrument["SLV"].unit_max_loss == Decimal("2.8000")
    assert by_instrument["AGQ"].structure == "levered_etf"
    assert by_instrument["AGQ"].unit_max_loss == Decimal("8.0000")
    assert options.asked == [("SLV", "call")]
    short = asyncio.run(
        build_menu(
            subject,
            "short",
            Decimal("33"),
            proxies,
            set(),
            RISK.levered_funds,
            options,
            35,
            TODAY,
            allowed,
        )
    )
    assert {c.structure for c in short} == {"long_put"}
    crossed = asyncio.run(
        build_menu(
            subject,
            "long",
            Decimal("31"),
            proxies,
            set(),
            RISK.levered_funds,
            options,
            35,
            TODAY,
            allowed,
        )
    )
    assert crossed == []


def idea(**overrides: object) -> Idea:
    fields: dict[str, object] = {
        "source_id": uuid4(),
        "source_kind": "thesis",
        "extra_parents": (),
        "subject": "SLV",
        "direction": "long",
        "summary": "SLV long",
        "origin": "commodity",
        "conviction": 4,
        "stop": Decimal("27"),
        "stop_ref": "levels:SLV:low_20d",
        "thesis_hard": Decimal("27"),
        "review_by": date(2026, 10, 30),
        "catalyst_names": (),
    }
    fields.update(overrides)
    return Idea(**fields)  # type: ignore[arg-type]


def test_trader_reply_checks() -> None:
    choices = option_choices([quote("30", "1.10", "1.20")], 1)
    levels = [
        Level("lvl_1", "SLV", "high_20d", "SLV 20-day high", Decimal("33"), "levels:SLV:high_20d"),
        Level("lvl_2", "SLV", "low_20d", "SLV 20-day low", Decimal("27"), "levels:SLV:low_20d"),
    ]
    table = FactTable(menu_facts(choices, levels, idea()))
    check = check_reply(choices, levels, Decimal("30"), idea(), table)
    good = TraderReply(
        choice_id="ch_1", target_level_id="lvl_1", rationale="Defined risk at {ch_1_cost}."
    )
    assert check(good) == []
    assert any("choice_id" in p for p in check(good.model_copy(update={"choice_id": "ch_9"})))
    assert any(
        "above the entry" in p for p in check(good.model_copy(update={"target_level_id": "lvl_2"}))
    )
    assert any(
        "'115'" in p for p in check(good.model_copy(update={"rationale": "Costs 115 dollars."}))
    )


# --- End to end ---------------------------------------------------------------------------


class ScriptedTrader:
    """Picks the first menu choice with no target and writes a short risk note."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def unload(self, model: str) -> None:
        return None

    async def structured(self, model: Any, prompt: Any, schema: Any, check: Any) -> Any:
        self.calls.append(schema.__name__)
        if schema.__name__ == "TraderReply":
            value = schema.model_validate(
                {"choice_id": "ch_1", "target_level_id": None, "rationale": "Simple and liquid."}
            )
        else:
            value = schema.model_validate({"note": "The loss is capped at {max_loss}."})
        assert check(value) == [], check(value)
        return StructuredResult(value, Usage(model=model.model), 1)


def seed(engine: Engine, now: datetime) -> Thesis:
    condition = {"instrument": "TSTTR", "operator": "below", "level_ref": "levels:TSTTR:low_20d"}
    thesis = Thesis.model_validate(
        {
            "produced_by": "idea.writer",
            "runtime_ms": 0,
            "parents": [],
            "statement": "TSTTR keeps rising.",
            "origin": "jon",
            "instruments": ["TSTTR"],
            "direction": "long",
            "horizon": "weeks",
            "review_by": (now + timedelta(days=30)).date(),
            "drivers": [{"statement": "Orders", "metric": "orders", "source": "edgar"}],
            "invalidation": {
                "warning": {**condition, "measure": "intraday_price", "level": "97"},
                "hard": {**condition, "measure": "daily_close", "level": "95"},
            },
            "conviction": 4,
            "state": "active",
        }
    )
    today = now.astimezone(NEW_YORK).date()
    with engine.begin() as conn:
        append_artifact(conn, thesis)
        for offset in range(40, 0, -1):
            day = today - timedelta(days=offset)
            close = Decimal(100 + (offset % 4))
            conn.execute(
                text(
                    "INSERT INTO price_bars (source, symbol, interval, ts, open, high, low, close, "
                    "fetched_at) VALUES ('yahoo', 'TSTTR', '1d', :ts, :c, :h, :l, :c, now())"
                ),
                {
                    "ts": datetime.combine(day, time(0), NEW_YORK),
                    "c": close,
                    "h": close + 4,
                    "l": close - 4,
                },
            )
        ingest(
            conn,
            CollectResult(
                accounts=[
                    AccountSnapshot(
                        source="ibkr_flex",
                        broker="ibkr",
                        account_ref="ibkr:0077",
                        as_of=now,
                        fetched_at=now,
                        net_liquidation=Decimal(5000),
                        cash=Decimal(300),
                        settled_cash=Decimal(300),
                        buying_power=None,
                        currency="USD",
                        positions=(),
                    )
                ]
            ),
        )
    return thesis


def pursue(engine: Engine, thesis: Thesis) -> DebateVerdict:
    # A verdict's reasons must cite a verified claim, so the chain is seeded too.
    record = RawRecord(
        produced_by="data.test",
        runtime_ms=0,
        source="finnhub.company_news",
        source_id=str(uuid4()),
        fetched_at=datetime.now(UTC),
        tickers=("TSTTR",),
        payload={"headline": "TSTTR", "summary": "TSTTR orders rose."},
        content_hash=content_hash("finnhub.company_news", str(uuid4())),
    )
    claim = Claim(
        produced_by="research",
        runtime_ms=0,
        parents=(record.id,),
        subject="TSTTR",
        statement="TSTTR orders rose.",
        source_record_id=record.id,
        quoted_span="TSTTR orders rose",
    )
    verified = VerifiedClaim(
        produced_by="factcheck",
        runtime_ms=0,
        parents=(claim.id,),
        claim_id=claim.id,
        verdict="verified",
        reason="ok",
    )
    point = Point(text="Orders rose.", claim_ids=(verified.id,))
    verdict = DebateVerdict(
        produced_by="analyst.judge",
        runtime_ms=0,
        parents=(thesis.id, verified.id),
        thesis_id=thesis.id,
        subject="TSTTR",
        view_ids=(),
        bull=(point,),
        bear=(point,),
        rebuttal=(point,),
        rubric=Rubric(evidence_quality="strong", rebuttal="adequate", risk_reward="good"),
        verdict="pursue",
        confidence_label="high",
        reasons=(point,),
        dissent="Orders may slow.",
    )
    with engine.begin() as conn:
        for artifact in (record, claim, verified, verdict):
            append_artifact(conn, artifact)
    return verdict


def test_pre_market_plans_and_sizes_pursued_theses(db_engine: Engine) -> None:
    now = datetime.now(UTC)
    thesis = seed(db_engine, now)
    verdict = pursue(db_engine, thesis)
    chat = ScriptedTrader()
    run = run_trader_desk(
        db_engine,
        chat,  # type: ignore[arg-type]
        load_models().deep,
        None,
        RISK,
        load_trader_config(),
        load_tiers(),
        load_universe(),
        NEW_YORK,
        now,
        now + timedelta(hours=1),
    )
    outcome = asyncio.run(run)
    assert any(note.startswith("plan TSTTR") for note in outcome.notes), outcome
    with db_engine.connect() as conn:
        plan_id = conn.execute(
            text(
                "SELECT p.child_id FROM artifact_parents p JOIN artifacts a ON a.id = p.child_id "
                "WHERE p.parent_id = :v AND a.kind = 'trade_plan'"
            ),
            {"v": verdict.id},
        ).scalar_one()
        plan = get_artifact(conn, plan_id)
        decision_id = conn.execute(
            text(
                "SELECT id FROM artifacts WHERE kind = 'risk_decision' AND payload->>'plan_id' = :p"
            ),
            {"p": str(plan_id)},
        ).scalar_one()
        decision = get_artifact(conn, decision_id)
    assert isinstance(plan, TradePlan) and isinstance(decision, RiskDecision)
    assert plan.thesis_id == thesis.id and plan.stop == Decimal("95")
    assert plan.entry_ref.startswith("price_bars:yahoo:TSTTR")
    assert {c.name for c in decision.checks} >= {
        "instrument_allowed",
        "max_loss",
        "stop_consistent",
    }
    assert decision.decision in ("approved", "resized", "vetoed")
    if decision.decision != "vetoed":
        assert decision.max_loss <= decision.cap

    client = TestClient(create_app(db_engine, ollama_base_url="http://127.0.0.1:9"))
    page = client.get(f"/plans/{plan_id}")
    assert page.status_code == 200 and "instrument_allowed" in page.text
    trader = client.get("/dev/status").json()["trader"]
    assert any(p["subject"] == "TSTTR" for p in trader["plans"])

    # Planned once per verdict.
    again = asyncio.run(
        run_trader_desk(
            db_engine,
            ScriptedTrader(),  # type: ignore[arg-type]
            load_models().deep,
            None,
            RISK,
            load_trader_config(),
            load_tiers(),
            load_universe(),
            NEW_YORK,
            now,
            now + timedelta(hours=1),
        )
    )
    assert not any(note.startswith("plan TSTTR") for note in again.notes)
