"""The pre-market shift's analyst work: holdings ratings, then due thesis debates.

Runs on the deep model before the morning briefing. Work stops starting new ratings or
debates at `deadline`, so the briefing shift always finds a free GPU; anything left is
due again at the next pre-market shift.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import Engine

from desk.artifacts.thesis import Thesis
from desk.config import ChatModel, TiersConfig, UniverseConfig
from desk.desks.analyst.debate import debate_thesis, due_debates, personas_for
from desk.desks.analyst.personas import Persona, keeper_for
from desk.desks.holdings.flags import HoldingsConfig, load_holdings, risk_flags
from desk.desks.holdings.rating import rate_holding
from desk.desks.idea.lanes import group_of
from desk.desks.idea.status import open_theses, warning_flags
from desk.llm.client import OllamaChat
from desk.watch.calendar import MarketCalendar

CLAIM_WINDOW = timedelta(days=5)


@dataclass
class AnalystOutcome:
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def is_stock(symbol: str, tiers: TiersConfig, universe: UniverseConfig) -> bool:
    stocks = {s for group in tiers.tier_1.values() for s in group.stocks}
    return symbol in stocks or symbol in universe.symbols


def linked_thesis(theses: list[Thesis], symbol: str) -> Thesis | None:
    """The open thesis on this instrument, preferring Jon's own."""
    matching = [t for t in theses if t.primary_instrument == symbol and t.state != "draft"]
    matching.sort(key=lambda t: (t.origin != "jon", t.created_at))
    return matching[0] if matching else None


async def run_analyst_desks(
    engine: Engine,
    chat: OllamaChat,
    model: ChatModel,
    personas: dict[str, Persona],
    tiers: TiersConfig,
    universe: UniverseConfig,
    holdings_config: HoldingsConfig,
    calendar: MarketCalendar,
    now: datetime,
    deadline: datetime,
    shift_id: UUID | None = None,
) -> AnalystOutcome:
    outcome = AnalystOutcome()
    tz = calendar.tz
    today = now.astimezone(tz).date()
    claims_since = now - CLAIM_WINDOW
    groups = group_of(tiers)
    with engine.connect() as conn:
        holdings = load_holdings(conn, tz)
        theses = open_theses(conn)
        warned = {flag.thesis_id for flag in warning_flags(conn, tz)}
        due = due_debates(conn, theses, warned, now)

    rated, forced = 0, 0
    for holding in holdings:
        if datetime.now(now.tzinfo) >= deadline:
            outcome.notes.append(f"ratings stopped at the deadline; {len(holdings) - rated} left")
            break
        stock = is_stock(holding.symbol, tiers, universe)
        keeper = keeper_for(personas, holding.symbol, groups.get(holding.symbol), stock)
        result = await rate_holding(
            engine,
            chat,
            model,
            keeper,
            holding,
            risk_flags(holding, holdings_config, today),
            linked_thesis(theses, holding.symbol),
            now,
            tz,
            claims_since,
            shift_id,
        )
        rated += 1
        if result.error:
            outcome.errors.append(result.error)
        elif result.rating is not None and result.rating.judged_rating is not None:
            forced += 1
    outcome.notes.append(
        f"ratings: {rated} of {len(holdings)} holdings"
        + (f", {forced} forced by risk flags" if forced else "")
        + (f", {len(outcome.errors)} failed" if outcome.errors else "")
    )

    debated, failed = 0, 0
    for item in due:
        if datetime.now(now.tzinfo) >= deadline:
            outcome.notes.append(f"debates stopped at the deadline; {len(due) - debated} left")
            break
        thesis = item.thesis
        chosen = personas_for(
            personas,
            thesis,
            groups.get(thesis.primary_instrument),
            is_stock(thesis.primary_instrument, tiers, universe),
        )
        result = await debate_thesis(
            engine, chat, model, chosen, thesis, now, tz, claims_since, shift_id
        )
        debated += 1
        if result.error:
            failed += 1
            outcome.errors.append(result.error)
        elif result.verdict is not None:
            outcome.notes.append(
                f"debate {thesis.primary_instrument} ({item.reason}): {result.verdict.verdict}"
            )
    outcome.notes.append(
        f"debates: {debated - failed} of {len(due)} due" + (f", {failed} failed" if failed else "")
    )
    return outcome
