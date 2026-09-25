"""The trader and risk desks in the pre-market shift, after the debates.

Ideas are theses whose latest verdict is pursue and this morning's Buy/Add holding
ratings, each planned once. Accounts, open risk and events come from stored data:
existing holdings count at their thesis hard line or an assumed ATR stop, and plans
approved earlier count at their planned maximum loss.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, Engine, text

from desk.artifacts.analyst import HoldingRating
from desk.artifacts.store import append_artifact, get_artifact
from desk.artifacts.thesis import Thesis
from desk.collectors.holdings import latest_snapshots
from desk.config import ChatModel, TiersConfig, UniverseConfig
from desk.desks.analyst.debate import chain_ids, latest_verdicts
from desk.desks.idea.levels import atr, level_menu
from desk.desks.idea.status import open_theses
from desk.desks.risk.config import RiskConfig
from desk.desks.risk.rules import AccountState, OpenRisk, bucket_of, holding_open_risk
from desk.desks.trader.menu import Priced, TraderConfig, build_menu
from desk.desks.trader.options import OptionSource
from desk.desks.trader.plan import Idea, plan_idea
from desk.llm.client import OllamaChat
from desk.watch.scan import load_daily_bars

logger = logging.getLogger(__name__)

MAJOR_EVENT_KINDS = ("fomc", "cpi", "jobs")
RATING_WINDOW = timedelta(hours=12)


@dataclass
class TraderOutcome:
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def priced(conn: Connection, symbol: str, tz: ZoneInfo) -> Priced | None:
    """Live mid when the stream has a two-sided quote, else the last stored daily close."""
    row = conn.execute(
        text("SELECT bid, ask FROM quotes_latest WHERE symbol = :s"), {"s": symbol}
    ).first()
    if row is not None and row.bid and row.ask and row.bid > 0 and row.ask > 0:
        mid = ((Decimal(row.bid) + Decimal(row.ask)) / 2).quantize(Decimal("0.01"))
        return Priced(symbol, mid, f"quotes_latest:{symbol}")
    bar = conn.execute(
        text(
            "SELECT ts, close FROM price_bars WHERE source = 'yahoo' AND interval = '1d' "
            "AND symbol = :s ORDER BY ts DESC LIMIT 1"
        ),
        {"s": symbol},
    ).first()
    if bar is None:
        return None
    day = bar.ts.astimezone(tz).date()
    return Priced(
        symbol, Decimal(bar.close).quantize(Decimal("0.01")), f"price_bars:yahoo:{symbol}:1d:{day}"
    )


def has_plan(conn: Connection, parent: UUID) -> bool:
    return bool(
        conn.execute(
            text(
                "SELECT 1 FROM artifact_parents p JOIN artifacts a ON a.id = p.child_id "
                "WHERE p.parent_id = :id AND a.kind = 'trade_plan' LIMIT 1"
            ),
            {"id": parent},
        ).first()
    )


def thesis_ideas(conn: Connection) -> list[Idea]:
    verdicts = latest_verdicts(conn)
    ideas = []
    for thesis in open_theses(conn):
        if (
            thesis.state != "active"
            or thesis.direction == "relative"
            or thesis.invalidation is None
        ):
            continue
        latest = next((verdicts[v] for v in chain_ids(conn, thesis) if v in verdicts), None)
        if latest is None or latest[1] != "pursue" or has_plan(conn, latest[0]):
            continue
        hard = thesis.invalidation.hard
        ideas.append(
            Idea(
                source_id=thesis.id,
                source_kind="thesis",
                extra_parents=(latest[0],),
                subject=thesis.primary_instrument,
                direction="long" if thesis.direction == "long" else "short",
                summary=f"{thesis.primary_instrument} {thesis.direction}: {thesis.statement}",
                origin=thesis.origin,
                conviction=thesis.conviction or 1,
                stop=hard.level,
                stop_ref=hard.level_ref,
                thesis_hard=hard.level,
                review_by=thesis.review_by,
                catalyst_names=tuple(c.name for c in thesis.catalysts),
            )
        )
    return ideas


def rating_ideas(
    conn: Connection, config: TraderConfig, now: datetime, tz: ZoneInfo, theses: dict[UUID, Thesis]
) -> list[Idea]:
    ids = conn.execute(
        text(
            "SELECT id FROM artifacts WHERE kind = 'holding_rating' AND status = 'ok' "
            "AND payload->>'rating' = 'buy_add' AND created_at >= :since"
        ),
        {"since": now - RATING_WINDOW},
    ).scalars()
    ideas = []
    for rating_id in ids:
        rating = get_artifact(conn, rating_id)
        assert isinstance(rating, HoldingRating)
        if has_plan(conn, rating.id):
            continue
        thesis = theses.get(rating.thesis_id) if rating.thesis_id else None
        if thesis is not None and thesis.invalidation is not None:
            stop, stop_ref, hard = (
                thesis.invalidation.hard.level,
                thesis.invalidation.hard.level_ref,
                thesis.invalidation.hard.level,
            )
        else:
            bars = load_daily_bars(
                conn, [rating.subject], now.astimezone(tz).date() + timedelta(days=1), tz
            )
            level = next(
                (
                    lv
                    for lv in level_menu(rating.subject, bars.get(rating.subject, []))
                    if lv.name == "close_minus_2atr"
                ),
                None,
            )
            if level is None:
                continue
            stop, stop_ref, hard = level.value, level.ref, None
        ideas.append(
            Idea(
                source_id=rating.id,
                source_kind="rating",
                extra_parents=(),
                subject=rating.subject,
                direction="long",
                summary=f"Add to {rating.subject}: {rating.reasons[0].text}",
                origin="holdings",
                conviction=config.rating_conviction.get(rating.confidence_label, 2),
                stop=stop,
                stop_ref=stop_ref,
                thesis_hard=hard,
                review_by=thesis.review_by if thesis else None,
                catalyst_names=tuple(c.name for c in thesis.catalysts) if thesis else (),
            )
        )
    return ideas


def account_states(conn: Connection, config: RiskConfig) -> list[AccountState]:
    states = []
    for snapshot in latest_snapshots(conn):
        broker = snapshot["account_ref"].split(":", 1)[0]
        account_type = config.accounts.get(broker)
        if account_type is None:
            continue
        states.append(
            AccountState(
                ref=snapshot["account_ref"],
                account_type=account_type,
                net_liq=Decimal(snapshot["net_liquidation"] or 0),
                settled_cash=Decimal(snapshot["settled_cash"] or 0),
            )
        )
    return states


def open_risks(
    conn: Connection, config: RiskConfig, theses: list[Thesis], today: date, tz: ZoneInfo
) -> list[OpenRisk]:
    hard_by_symbol = {
        t.primary_instrument: t.invalidation.hard.level
        for t in theses
        if t.invalidation is not None and t.direction == "long" and t.state != "draft"
    }
    risks = []
    for snapshot in latest_snapshots(conn):
        for position in snapshot["positions"]:
            if not position["quantity"] or position["mark_price"] is None:
                continue
            symbol = position["symbol"]
            bars = load_daily_bars(conn, [symbol], today + timedelta(days=1), tz).get(symbol, [])
            step = atr(bars)
            risks.append(
                holding_open_risk(
                    symbol,
                    Decimal(position["quantity"]),
                    Decimal(position["mark_price"]),
                    Decimal(str(step)) if step is not None else None,
                    hard_by_symbol.get(symbol),
                    config,
                )
            )
    for row in conn.execute(
        text(
            "SELECT p.payload->>'subject' AS subject, (d.payload->>'max_loss')::numeric AS loss "
            "FROM artifacts d JOIN artifacts p ON p.id = (d.payload->>'plan_id')::uuid "
            "WHERE d.kind = 'risk_decision' AND d.payload->>'decision' IN ('approved', 'resized') "
            "AND d.created_at >= :since"
        ),
        {"since": datetime.combine(today, datetime.min.time(), tz)},
    ):
        risks.append(
            OpenRisk(row.subject, bucket_of(row.subject, config), Decimal(row.loss), assumed=False)
        )
    return risks


def events_for(
    conn: Connection, symbol: str, today: date, end: date
) -> tuple[list[date], list[tuple[date, str]]]:
    earnings = [
        date.fromisoformat(value)
        for value in conn.execute(
            text(
                "SELECT DISTINCT payload->'payload'->>'date' FROM artifacts "
                "WHERE kind = 'raw_record' AND payload->>'source' = 'finnhub.earnings_calendar' "
                "AND payload->'payload'->>'symbol' = :s"
            ),
            {"s": symbol},
        ).scalars()
        if value
    ]
    majors = [
        (row.at.date(), row.name)
        for row in conn.execute(
            text(
                "SELECT name, at FROM calendar_events WHERE kind = ANY(:kinds) "
                "AND at >= :start AND at < :end ORDER BY at"
            ),
            {"kinds": list(MAJOR_EVENT_KINDS), "start": today, "end": end + timedelta(days=1)},
        )
    ]
    return earnings, majors


async def run_trader_desk(
    engine: Engine,
    chat: OllamaChat,
    model: ChatModel,
    options: OptionSource | None,
    risk_config: RiskConfig,
    trader_config: TraderConfig,
    tiers: TiersConfig,
    universe: UniverseConfig,
    tz: ZoneInfo,
    now: datetime,
    deadline: datetime,
    shift_id: UUID | None = None,
) -> TraderOutcome:
    outcome = TraderOutcome()
    today = now.astimezone(tz).date()
    stocks = {s for group in tiers.tier_1.values() for s in group.stocks} | set(universe.symbols)
    with engine.connect() as conn:
        theses = open_theses(conn)
        ideas = thesis_ideas(conn) + rating_ideas(
            conn, trader_config, now, tz, {t.id: t for t in theses}
        )
        accounts = [a for a in account_states(conn, risk_config) if a.net_liq > 0]
        risks = open_risks(conn, risk_config, theses, today, tz)
    if not ideas:
        outcome.notes.append("trader: no pursued theses or Buy/Add ratings to plan")
        return outcome
    if not accounts:
        outcome.notes.append("trader: no funded account")
        return outcome
    account = max(accounts, key=lambda a: a.net_liq)
    combined = sum((a.net_liq for a in accounts), Decimal(0))
    rules = risk_config.account_types[account.account_type]
    allowed = set(rules.structures) | ({"short_shares"} if rules.allow_short_shares else set())
    by_thesis = {t.id: t for t in theses}

    counts = {"approved": 0, "resized": 0, "vetoed": 0}
    for idea in ideas:
        if datetime.now(now.tzinfo) >= deadline:
            outcome.notes.append("trader stopped at the deadline")
            break
        with engine.connect() as conn:
            entry = priced(conn, idea.subject, tz)
            proxy_symbols: list[str] = []
            thesis = by_thesis.get(idea.source_id)
            if thesis is not None:
                proxy_symbols += list(thesis.instruments[1:])
            etf = trader_config.future_proxies.get(idea.subject)
            if etf:
                proxy_symbols.append(etf)
            for base in (idea.subject, etf):
                if base:
                    proxy_symbols += list(trader_config.levered_proxies.get(base, ()))
            proxies = [p for p in (priced(conn, s, tz) for s in dict.fromkeys(proxy_symbols)) if p]
            bars = load_daily_bars(conn, [idea.subject], today + timedelta(days=1), tz).get(
                idea.subject, []
            )
            levels = [lv for lv in level_menu(idea.subject, bars) if lv.name != "last_close"]
            end = idea.review_by or today + timedelta(days=trader_config.default_horizon_days)
            earnings, majors = events_for(conn, idea.subject, today, end)
        if entry is None:
            outcome.errors.append(f"{idea.subject}: no price to plan from")
            continue
        horizon = max((end - today).days, 7)
        choices = await build_menu(
            entry,
            idea.direction,
            idea.stop,
            proxies,
            stocks,
            risk_config.levered_funds,
            options,
            horizon,
            today,
            allowed,
        )
        if not choices:
            outcome.errors.append(
                f"{idea.subject}: nothing on the menu "
                "(stop already crossed or no allowed structure)"
            )
            continue
        plan, decision, error = await plan_idea(
            chat,
            model,
            idea,
            choices,
            levels,
            entry,
            account,
            risks,
            combined,
            risk_config,
            today,
            earnings,
            majors,
            shift_id,
        )
        if error or plan is None or decision is None:
            outcome.errors.append(error or f"{idea.subject}: planning failed")
            continue
        with engine.begin() as conn:
            append_artifact(conn, plan)
            append_artifact(conn, decision)
        counts[decision.decision] += 1
        if decision.decision != "vetoed":
            risks.append(
                OpenRisk(
                    plan.subject,
                    bucket_of(plan.subject, risk_config),
                    decision.max_loss,
                    assumed=False,
                )
            )
        outcome.notes.append(
            f"plan {idea.subject} ({plan.structure} {plan.instrument}): {decision.decision}"
            + (f", size {decision.size}" if decision.size else f": {decision.veto_reasons[0]}")
        )
    outcome.notes.append(
        f"trader: {counts['approved']} approved, {counts['resized']} resized, "
        f"{counts['vetoed']} vetoed"
    )
    return outcome
