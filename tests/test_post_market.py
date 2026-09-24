import asyncio
import re
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, text

from desk.artifacts.raw_record import RawRecord, content_hash
from desk.artifacts.store import append_artifact, get_artifact
from desk.artifacts.thesis import Thesis
from desk.artifacts.trigger import Trigger
from desk.config import load_models, load_tiers, load_universe
from desk.desks.factcheck import SupportReply
from desk.desks.idea.lanes import load_lanes_config
from desk.desks.idea.run import run_post_market
from desk.desks.idea.writer import ThesisDraft
from desk.desks.research import ResearchDraft
from desk.llm.client import StructuredResult, Usage
from desk.watch.calendar import MarketCalendar, load_calendar_config
from desk.watch.rules import load_watch_config

SYMBOL = "TSTPM"
SENTENCE = "TSTPM won a large multi-year order from a regional utility."


class ScriptedChat:
    """Answers each desk's schema the way a well-behaved model would."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.unloaded: list[str] = []

    async def unload(self, model: str) -> None:
        self.unloaded.append(model)

    async def structured(self, model: Any, prompt: Any, schema: Any, check: Any) -> Any:
        self.calls.append(schema.__name__)
        value = getattr(self, f"_{schema.__name__}")(prompt.user)
        problems = check(value)
        assert problems == [], problems
        return StructuredResult(value, Usage(model=model.model, tokens_out=10), 1)

    def _ResearchDraft(self, user: str) -> ResearchDraft:
        match = re.search(r"^\[(\d+)\] \([^)]*\)\n.*" + re.escape(SENTENCE), user, re.MULTILINE)
        if f"Subject: {SYMBOL}" not in user or match is None:
            return ResearchDraft(sections=[], claims=[])
        return ResearchDraft.model_validate(
            {
                "sections": [{"title": "What happened", "text": "A large order [c1]."}],
                "claims": [
                    {
                        "statement": "TSTPM won a multi-year utility order.",
                        "source_index": int(match.group(1)),
                        "quote": SENTENCE,
                        "numbers": [],
                    }
                ],
            }
        )

    def _SupportReply(self, user: str) -> SupportReply:
        indexes = re.findall(r"^\[(\d+)\] statement:", user, re.MULTILINE)
        return SupportReply.model_validate(
            {
                "labels": [
                    {"index": int(i), "support": "strong", "reason": "Direct"} for i in indexes
                ]
            }
        )

    def _ThesisDraft(self, user: str) -> ThesisDraft:
        levels = {
            m.group(1): (Decimal(m.group(2).replace(",", "")), m.group(3))
            for m in re.finditer(r"^\{(lvl_\d+)\} = \$([\d,.]+)  \(\S+ (.+)\)$", user, re.MULTILINE)
        }
        last = next(v for v, label in levels.values() if label == "last close")
        below = sorted((v, k) for k, (v, _) in levels.items() if v < last)
        return ThesisDraft.model_validate(
            {
                "statement": "The utility order supports further gains.",
                "direction": "long",
                "horizon": "weeks",
                "drivers": [
                    {"statement": "Orders keep coming", "metric": "orders", "source": "edgar"}
                ],
                "evidence_ids": ["claim_1"],
                "warning_level_id": below[-1][1],
                "hard_level_id": below[0][1],
            }
        )


def seed(engine: Engine, now: datetime, tz: Any) -> None:
    record = RawRecord(
        produced_by="data.test",
        runtime_ms=0,
        source="finnhub.company_news",
        source_id=str(uuid4()),
        fetched_at=now,
        tickers=(SYMBOL,),
        payload={"headline": "TSTPM order", "summary": SENTENCE},
        content_hash=content_hash("finnhub.company_news", str(uuid4())),
    )
    trigger = Trigger(
        produced_by="watch.test",
        runtime_ms=0,
        rule_id="range_break",
        instrument=SYMBOL,
        tier=None,
        importance=0.95,
        urgent=False,
        summary=f"{SYMBOL} broke above its 20-day range",
        fingerprint=f"test:{uuid4()}",
    )
    today = now.astimezone(tz).date()
    with engine.begin() as conn:
        append_artifact(conn, record)
        append_artifact(conn, trigger)
        for offset in range(40, 0, -1):
            day = today - timedelta(days=offset)
            close = Decimal(100 + (offset % 5))
            conn.execute(
                text(
                    "INSERT INTO price_bars (source, symbol, interval, ts, open, high, low, "
                    "close, fetched_at) VALUES ('yahoo', :s, '1d', :ts, :c, :h, :l, :c, now())"
                ),
                {
                    "s": SYMBOL,
                    "ts": datetime.combine(day, time(0), tz),
                    "c": close,
                    "h": close + 2,
                    "l": close - 2,
                },
            )


def test_post_market_runs_lanes_through_theses(db_engine: Engine) -> None:
    calendar = MarketCalendar(load_calendar_config())
    now = datetime.now(UTC)
    seed(db_engine, now, calendar.tz)
    models = load_models()
    chat = ScriptedChat()
    outcome = asyncio.run(
        run_post_market(
            db_engine,
            chat,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            models,
            load_lanes_config(),
            load_watch_config(),
            load_tiers(),
            load_universe(),
            calendar,
            now,
        )
    )
    assert outcome.error is None, outcome.notes
    assert {"ResearchDraft", "SupportReply", "ThesisDraft"} <= set(chat.calls)
    # Deep model released after research and after theses; small after fact-check.
    assert chat.unloaded.count(models.deep.model) == 2
    assert chat.unloaded.count(models.small.model) == 1

    with db_engine.connect() as conn:
        thesis_id = conn.execute(
            text(
                "SELECT id FROM artifacts WHERE kind = 'thesis' AND status = 'ok' "
                "AND payload->'instruments'->>0 = :s"
            ),
            {"s": SYMBOL},
        ).scalar_one()
        thesis = get_artifact(conn, thesis_id)
        assert isinstance(thesis, Thesis)
        assert thesis.origin == "screen" and thesis.state == "active"
        assert thesis.invalidation is not None
        assert thesis.invalidation.hard.level < thesis.invalidation.warning.level
        assert thesis.invalidation.hard.level_ref.startswith(f"levels:{SYMBOL}:")
        assert len(thesis.evidence) == 1
        selection = conn.execute(
            text(
                "SELECT payload FROM artifacts WHERE kind = 'idea_selection' "
                "ORDER BY created_at DESC LIMIT 1"
            )
        ).scalar_one()
    candidate_ids = [p for p in thesis.parents if p not in thesis.evidence]
    assert any(str(c) in selection["selected"] for c in candidate_ids)
    assert any(note.startswith("theses: 1 written") for note in outcome.notes)
