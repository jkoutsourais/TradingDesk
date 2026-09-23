"""Morning briefing: code gathers facts, the deep model writes around placeholders, code
renders the numbers, then the brief is stored and a generic push links to it.

Coverage runs from the previous trading day's close to now, so Monday's briefing covers
the weekend. If the model fails twice the brief is still written, with status failed and
code-built sections, so the morning page and push never go missing.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import Connection, Engine, text

from desk.artifacts.base import ArtifactStatus
from desk.artifacts.brief import Brief, BriefSection, FactSnapshot, SnapshotFact
from desk.artifacts.store import append_artifact
from desk.collectors.holdings import latest_snapshots
from desk.config import ChatModel
from desk.front_office.notify import NotifyConfig, NtfyClient, record_push
from desk.llm.client import OllamaChat, StructuredResult
from desk.llm.facts import Fact, FactTable
from desk.llm.prompts import load_prompt
from desk.watch.calendar import MarketCalendar

logger = logging.getLogger(__name__)

MARKET_SYMBOLS = (
    ("SPY", "S&P 500 ETF"),
    ("QQQ", "Nasdaq 100 ETF"),
    ("/GC", "gold futures"),
    ("/SI", "silver futures"),
    ("/HG", "copper futures"),
    ("/CL", "WTI crude futures"),
    ("/NG", "natural gas futures"),
    ("XLE", "energy sector ETF"),
    ("XLU", "utilities sector ETF"),
    ("^VIX", "VIX volatility index"),
    ("DX-Y.NYB", "US dollar index"),
)
FRED_SERIES = (
    ("DGS2", "2-year Treasury yield"),
    ("DGS10", "10-year Treasury yield"),
    ("DFII10", "10-year real yield"),
    ("T10Y2Y", "10-year minus 2-year curve"),
)
MAX_TRIGGERS = 12
MAX_NEWS = 10
QUOTE_FRESH = timedelta(minutes=30)


class BriefDraft(BaseModel):
    headline: str = Field(min_length=1, max_length=400)
    market: list[str] = Field(max_length=6)
    holdings: list[str] = Field(max_length=30)
    watch: list[str] = Field(max_length=8)


# --- Formatting (code owns every displayed number) ---------------------------------------


def _pct(value: Decimal) -> str:
    return f"{value:+.2f}%"


def _money(value: Decimal) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def _price(value: Decimal) -> str:
    return f"{value:,.4f}" if abs(value) < 1 else f"{value:,.2f}"


def _bp(value: Decimal) -> str:
    return f"{value * 100:+.0f} bp"


def _q(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.0001"))


# Fact ids the model can copy reliably. It drops leading "/" and "^" when it writes them,
# so futures and indexes get plain keys; futures are prefixed because some roots are also
# stock tickers (CL is Colgate-Palmolive).
_ID_ALIASES = {"^VIX": "VIX", "DX-Y.NYB": "DXY"}


def fact_key(symbol: str) -> str:
    if symbol in _ID_ALIASES:
        return _ID_ALIASES[symbol]
    if symbol.startswith("/"):
        return f"fut_{symbol[1:]}"
    return symbol


# --- Fact gathering -----------------------------------------------------------------------


@dataclass
class BriefingInputs:
    facts: list[Fact] = field(default_factory=list)
    holdings_order: list[str] = field(default_factory=list)
    trigger_ids: list[UUID] = field(default_factory=list)
    news_ids: list[UUID] = field(default_factory=list)
    calendar_ids: list[str] = field(default_factory=list)


def _last_and_prior_close(conn: Connection, symbol: str, before: datetime) -> list[Any]:
    return conn.execute(
        text(
            "SELECT ts, close FROM price_bars WHERE source = 'yahoo' AND interval = '1d' "
            "AND symbol = :s AND ts < :before ORDER BY ts DESC LIMIT 2"
        ),
        {"s": symbol, "before": before},
    ).all()


def _live_price(conn: Connection, symbol: str, now: datetime) -> tuple[Decimal, str] | None:
    row = conn.execute(
        text("SELECT bid, ask, quote_time FROM quotes_latest WHERE symbol = :s"), {"s": symbol}
    ).one_or_none()
    if row is None or row.bid is None or row.ask is None or row.quote_time is None:
        return None
    if now - row.quote_time > QUOTE_FRESH:
        return None
    return (row.bid + row.ask) / 2, f"quotes_latest:{symbol}:{row.quote_time.isoformat()}"


def _price_facts(
    conn: Connection, symbol: str, label: str, now: datetime, session_start: datetime
) -> list[Fact]:
    closes = _last_and_prior_close(conn, symbol, session_start)
    live = _live_price(conn, symbol, now)
    if not closes:
        return []
    last_close_ts, last_close = closes[0]
    close_ref = f"price_bars:yahoo:{symbol}:1d:{last_close_ts.date().isoformat()}"
    if live is not None:
        price, price_ref = live
        base, base_ref = last_close, close_ref
    elif len(closes) == 2:
        # No live quote (index or dollar): report the last completed day instead.
        price, price_ref = last_close, close_ref
        base = closes[1][1]
        base_ref = f"price_bars:yahoo:{symbol}:1d:{closes[1][0].date().isoformat()}"
    else:
        return []
    change = _q((price / base - 1) * 100) if base else Decimal(0)
    key = fact_key(symbol)
    return [
        Fact(f"{key}.price", _q(price), "USD", _price(price), f"{label} price", price_ref),
        Fact(
            f"{key}.change",
            change,
            "%",
            _pct(change),
            f"{label} change",
            f"computed:({price_ref})/({base_ref})",
        ),
    ]


def gather(
    conn: Connection, calendar: MarketCalendar, now: datetime, covers_from: datetime
) -> BriefingInputs:
    inputs = BriefingInputs()
    today = now.astimezone(calendar.tz).date()
    session_start = datetime.combine(today, time(0), calendar.tz)

    for snapshot in latest_snapshots(conn):
        ref = f"account_snapshots:{snapshot['id']}"
        tag = snapshot["account_ref"].replace(":", "_")
        if snapshot["net_liquidation"] is not None:
            nl = Decimal(snapshot["net_liquidation"])
            broker = snapshot["account_ref"].split(":", 1)[0]
            # Labels stay free of account digits, which the model may echo from a label.
            inputs.facts.append(
                Fact(
                    f"{tag}.net_liq",
                    nl,
                    "USD",
                    _money(nl),
                    f"{broker} account net liquidation",
                    ref,
                )
            )
        for p in snapshot["positions"]:
            symbol = p["symbol"]
            if symbol in inputs.holdings_order:
                continue
            inputs.holdings_order.append(symbol)
            inputs.facts += _price_facts(conn, symbol, symbol, now, session_start)
            if p["market_value"] is not None and p["cost_basis"] is not None:
                value, cost = Decimal(p["market_value"]), Decimal(p["cost_basis"])
                pnl = value - cost
                pnl_pct = _q(pnl / cost * 100) if cost else Decimal(0)
                pos_ref = f"position_snapshots:{snapshot['id']}:{symbol}"
                inputs.facts += [
                    Fact(
                        f"{fact_key(symbol)}.value",
                        value,
                        "USD",
                        _money(value),
                        f"{symbol} position value",
                        pos_ref,
                    ),
                    Fact(
                        f"{fact_key(symbol)}.pnl",
                        pnl,
                        "USD",
                        _money(pnl),
                        f"{symbol} unrealized gain or loss",
                        f"computed:value-cost({pos_ref})",
                    ),
                    Fact(
                        f"{fact_key(symbol)}.pnl_pct",
                        pnl_pct,
                        "%",
                        _pct(pnl_pct),
                        f"{symbol} unrealized gain or loss",
                        f"computed:pnl/cost({pos_ref})",
                    ),
                ]

    for symbol, label in MARKET_SYMBOLS:
        if symbol not in inputs.holdings_order:
            inputs.facts += _price_facts(conn, symbol, label, now, session_start)

    for series_id, label in FRED_SERIES:
        rows = conn.execute(
            text(
                "SELECT DISTINCT ON (period) period, value FROM series_observations "
                "WHERE source = 'fred' AND series_id = :id ORDER BY period DESC, fetched_at DESC "
                "LIMIT 2"
            ),
            {"id": series_id},
        ).all()
        if len(rows) == 2:
            (last_period, last), (_, prior) = rows
            ref = f"series_observations:fred:{series_id}:{last_period.isoformat()}"
            inputs.facts += [
                Fact(f"{series_id}.last", last, "%", f"{last:.2f}%", label, ref),
                Fact(
                    f"{series_id}.change",
                    last - prior,
                    "pp",
                    _bp(last - prior),
                    f"{label} daily change",
                    f"computed:diff({ref})",
                ),
            ]

    triggers = conn.execute(
        text(
            "SELECT id, payload->>'summary' AS summary, payload->>'rule_id' AS rule, "
            "payload->>'instrument' AS instrument FROM artifacts WHERE kind = 'trigger' "
            "AND created_at >= :since AND status = 'ok' "
            "ORDER BY coalesce((payload->>'tier')::int, 9), (payload->>'importance')::float DESC "
            "LIMIT :n"
        ),
        {"since": covers_from, "n": MAX_TRIGGERS},
    ).all()
    for index, row in enumerate(triggers, start=1):
        inputs.trigger_ids.append(row.id)
        inputs.facts.append(
            Fact(
                f"trig_{index}",
                row.summary,
                "",
                row.summary,
                f"watch hit ({row.rule}, {row.instrument})",
                f"trigger:{row.id}",
            )
        )

    news = conn.execute(
        text(
            "SELECT r.id, coalesce(r.payload->'payload'->>'headline', "
            "r.payload->'payload'->>'title', left(r.payload->'payload'->>'content', 200)) "
            "AS headline, r.payload->>'source' AS source "
            "FROM artifacts t JOIN artifacts r ON r.id = (t.payload->>'subject_id')::uuid "
            "WHERE t.kind = 'triage_label' AND t.payload->>'label' = 'relevant' "
            "AND r.kind = 'raw_record' AND r.created_at >= :since "
            "ORDER BY r.created_at DESC LIMIT :n"
        ),
        {"since": covers_from, "n": MAX_NEWS},
    ).all()
    for index, row in enumerate(news, start=1):
        inputs.news_ids.append(row.id)
        inputs.facts.append(
            Fact(
                f"news_{index}",
                row.headline,
                "",
                row.headline,
                f"headline ({row.source})",
                f"raw_record:{row.id}",
            )
        )

    events = conn.execute(
        text(
            "SELECT event_key, name, at FROM calendar_events WHERE at >= :start AND at < :end "
            "ORDER BY at"
        ),
        {"start": session_start, "end": session_start + timedelta(days=1)},
    ).all()
    for index, row in enumerate(events, start=1):
        display = f"{row.at.astimezone(calendar.tz):%H:%M} ET {row.name}"
        inputs.calendar_ids.append(f"cal_{index}")
        inputs.facts.append(
            Fact(
                f"cal_{index}",
                row.name,
                "",
                display,
                "scheduled release today",
                f"calendar_events:{row.event_key}",
            )
        )
    return inputs


# --- Build --------------------------------------------------------------------------------


def coverage_start(calendar: MarketCalendar, now: datetime) -> datetime:
    """The previous trading day's close: Monday's briefing reaches back to Friday."""
    today = now.astimezone(calendar.tz).date()
    session = calendar.session(calendar.previous_trading_day(today))
    assert session is not None
    return session.close


def _check(table: FactTable) -> Any:
    def check(draft: BriefDraft) -> list[str]:
        lines = [draft.headline, *draft.market, *draft.holdings, *draft.watch]
        return [problem for line in lines for problem in table.violations(line)]

    return check


def _fallback_sections(table: FactTable, inputs: BriefingInputs) -> list[BriefSection]:
    """Code-only brief used when the model fails: the same facts, without prose."""
    ids = {fact.id for fact in table}
    holdings = []
    for symbol in inputs.holdings_order:
        parts = [f"{symbol}"]
        if f"{fact_key(symbol)}.change" in ids:
            parts.append(f"{{{fact_key(symbol)}.change}} since the prior close")
        if f"{fact_key(symbol)}.pnl" in ids:
            key = fact_key(symbol)
            parts.append(f"unrealized {{{key}.pnl}} ({{{key}.pnl_pct}})")
        holdings.append(table.render(", ".join(parts)))
    watch = [table.render(f"{{trig_{i}}}") for i in range(1, len(inputs.trigger_ids) + 1)][:8]
    return [
        BriefSection(title="Your holdings", lines=tuple(holdings)),
        BriefSection(title="Watch hits", lines=tuple(watch)),
    ]


@dataclass
class BriefingOutcome:
    brief: Brief
    snapshot: FactSnapshot
    result: StructuredResult[BriefDraft] | None


async def build_briefing(
    engine: Engine,
    chat: OllamaChat,
    model: ChatModel,
    calendar: MarketCalendar,
    now: datetime,
    shift_id: UUID | None = None,
) -> BriefingOutcome:
    started = datetime.now(UTC)
    covers_from = coverage_start(calendar, now)
    with engine.connect() as conn:
        inputs = gather(conn, calendar, now, covers_from)
    table = FactTable(inputs.facts)
    local = now.astimezone(calendar.tz)
    is_monday = local.weekday() == 0
    prompt = load_prompt(
        "briefing",
        {
            "date_label": f"{local:%A %B} {local.day}",
            "window_label": "the weekend and Friday's close onward"
            if is_monday
            else "the previous close onward",
            "facts": table.prompt_listing(),
            "holdings_order": ", ".join(inputs.holdings_order) or "none",
        },
    )
    result = await chat.structured(model, prompt, BriefDraft, check=_check(table))

    snapshot = FactSnapshot(
        produced_by="front_office.briefing",
        runtime_ms=0,
        shift_id=shift_id,
        parents=(*inputs.trigger_ids, *inputs.news_ids),
        purpose="morning_brief",
        facts=tuple(
            SnapshotFact(
                id=f.id,
                value=f.value,
                unit=f.unit,
                display=f.display,
                label=f.label,
                source_ref=f.source_ref,
            )
            for f in table
        ),
    )
    calendar_lines = tuple(table.render(f"{{{cid}}}") for cid in inputs.calendar_ids) or (
        "No scheduled releases today.",
    )
    if result.value is not None:
        draft = result.value
        sections = [
            BriefSection(title="Summary", lines=(table.render(draft.headline),)),
            BriefSection(
                title="Market and regime", lines=tuple(table.render(line) for line in draft.market)
            ),
            BriefSection(
                title="Your holdings", lines=tuple(table.render(line) for line in draft.holdings)
            ),
            BriefSection(
                title="Watch hits", lines=tuple(table.render(line) for line in draft.watch)
            ),
        ]
        status, error = ArtifactStatus.OK, None
    else:
        sections = _fallback_sections(table, inputs)
        status, error = ArtifactStatus.FAILED, f"model failed twice: {result.error}"
    sections.append(BriefSection(title="Calendar", lines=calendar_lines))

    brief = Brief(
        produced_by="front_office.briefing",
        runtime_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
        shift_id=shift_id,
        parents=(snapshot.id,),
        model=model.model,
        prompt_version=prompt.version,
        tokens_in=result.usage.tokens_in,
        tokens_out=result.usage.tokens_out,
        status=status,
        error=error,
        brief_kind="weekend" if is_monday else "morning",
        covers_from=covers_from,
        covers_to=now,
        fact_snapshot_id=snapshot.id,
        sections=tuple(sections),
    )
    with engine.begin() as conn:
        append_artifact(conn, snapshot)
        append_artifact(conn, brief)
    return BriefingOutcome(brief, snapshot, result)


async def push_briefing(
    engine: Engine, ntfy: NtfyClient, config: NotifyConfig, brief: Brief
) -> str:
    title = (
        config.briefing.title
        if brief.status is ArtifactStatus.OK
        else (f"{config.briefing.title} (data only)")
    )
    click = f"{config.base_url}/briefs/{brief.id}"
    message = "Tap to read."
    try:
        await ntfy.send(
            title=title, message=message, priority=config.briefing.priority, click_url=click
        )
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"[:300]
        with engine.begin() as conn:
            record_push(
                conn,
                kind="briefing",
                ref_id=brief.id,
                title=title,
                message=message,
                click_url=click,
                priority=config.briefing.priority,
                status="failed",
                reason=reason,
            )
        raise
    with engine.begin() as conn:
        record_push(
            conn,
            kind="briefing",
            ref_id=brief.id,
            title=title,
            message=message,
            click_url=click,
            priority=config.briefing.priority,
            status="sent",
        )
    return click
