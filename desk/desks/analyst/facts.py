"""Fact sets for analyst prompts, all computed by code from stored data.

Each persona lists the sets it reads (personas/*.yaml). A fact has a placeholder id the
model cites, a display value and a source ref; points store the ref (or the verified
claim id for claim facts), so every citation resolves to stored data.
"""

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, text

from desk.desks.idea.lanes import load_grid_peaks
from desk.desks.idea.levels import atr, level_menu
from desk.desks.idea.writer import accepted_claims, claim_display
from desk.llm.facts import Fact, FactTable
from desk.watch.rules import DailyBar, percentile_rank, zscore
from desk.watch.scan import COT_INSTRUMENTS, EIA_INSTRUMENTS, load_daily_bars

MACRO_SERIES = {
    "DGS2": ("2-year Treasury yield", "%"),
    "DGS10": ("10-year Treasury yield", "%"),
    "DFII10": ("10-year real yield", "%"),
    "T10Y2Y": ("10-year minus 2-year curve", "%"),
    "DTWEXBGS": ("broad dollar index", "index"),
    "VIXCLS": ("VIX close", "index"),
}
MACRO_CHANGE_DAYS = 20
# ETF and producer proxies read the COT report of the future they track.
COT_PROXIES = {
    "GLD": "/GC",
    "UGL": "/GC",
    "GDX": "/GC",
    "GDXJ": "/GC",
    "NUGT": "/GC",
    "JNUG": "/GC",
    "NEM": "/GC",
    "AEM": "/GC",
    "SLV": "/SI",
    "SIL": "/SI",
    "AGQ": "/SI",
    "WPM": "/SI",
    "USO": "/CL",
    "XLE": "/CL",
    "XOP": "/CL",
    "OIH": "/CL",
    "UNG": "/NG",
    "EQT": "/NG",
}
POLICY_WINDOW = timedelta(hours=72)
MAX_POLICY = 6


@dataclass
class FactBook:
    """Facts for one subject plus the maps from fact id to what a citation stores."""

    facts: list[Fact] = field(default_factory=list)
    claims: dict[str, UUID] = field(default_factory=dict)  # claim_N -> VerifiedClaim id
    last_close: Decimal | None = None

    def add(self, fact: Fact) -> None:
        if all(existing.id != fact.id for existing in self.facts):
            self.facts.append(fact)

    def table(self) -> FactTable:
        return FactTable(self.facts)

    def ref(self, fact_id: str) -> str:
        return next(f.source_ref for f in self.facts if f.id == fact_id)

    def subset(self, sets: tuple[str, ...]) -> "FactBook":
        prefixes = {PREFIX[name] for name in sets}
        book = FactBook(claims=dict(self.claims), last_close=self.last_close)
        book.facts = [f for f in self.facts if f.id.split("_", 1)[0] in prefixes]
        return book


PREFIX = {
    "claims": "claim",
    "levels": "lvl",
    "trend": "trend",
    "macro": "macro",
    "cot": "cot",
    "eia": "eia",
    "grid": "grid",
    "policy": "pol",
}


def _pct(value: float) -> str:
    return f"{value:+.1f}%"


def add_claims(
    book: FactBook, conn: Connection, symbol: str, since: datetime, evidence: tuple[UUID, ...]
) -> None:
    rows = {row.id: row for row in accepted_claims(conn, symbol, since)}
    if evidence:
        for row in conn.execute(
            text(
                "SELECT v.id, c.payload->>'statement' AS statement, "
                "v.payload->>'verdict' AS verdict, v.payload->'recomputed' AS recomputed "
                "FROM artifacts v JOIN artifacts c ON c.id = (v.payload->>'claim_id')::uuid "
                "WHERE v.id = ANY(:ids)"
            ),
            {"ids": list(evidence)},
        ):
            rows.setdefault(row.id, row)
    for index, row in enumerate(rows.values(), start=1):
        fact_id = f"claim_{index}"
        book.claims[fact_id] = row.id
        display = claim_display(row.statement, row.verdict, row.recomputed or [])
        book.add(
            Fact(
                fact_id,
                display,
                "",
                display,
                f"verified claim on {symbol}",
                f"verified_claim:{row.id}",
            )
        )


def trend_facts(symbol: str, bars: list[DailyBar]) -> list[Fact]:
    if len(bars) < 2:
        return []
    last = bars[-1]
    ref = f"price_bars:yahoo:{symbol}:1d:{last.day}"
    facts = []
    for days in (20, 60):
        if len(bars) > days:
            change = (last.close / bars[-(days + 1)].close - 1) * 100
            facts.append(
                Fact(
                    f"trend_ret{days}",
                    Decimal(f"{change:.2f}"),
                    "%",
                    _pct(change),
                    f"{symbol} {days}-day return",
                    ref,
                )
            )
    step = atr(bars)
    if step is not None:
        share = step / last.close * 100
        facts.append(
            Fact(
                "trend_atr",
                Decimal(f"{share:.2f}"),
                "%",
                f"{share:.1f}%",
                f"{symbol} 20-day ATR as a share of price",
                ref,
            )
        )
    if len(bars) >= 120:
        high = max(b.high for b in bars[-252:])
        gap = (last.close / high - 1) * 100
        facts.append(
            Fact(
                "trend_from_high",
                Decimal(f"{gap:.2f}"),
                "%",
                _pct(gap),
                f"{symbol} distance from 52-week high",
                ref,
            )
        )
    if len(bars) >= 50:
        average = sum(b.close for b in bars[-50:]) / 50
        gap = (last.close / average - 1) * 100
        facts.append(
            Fact(
                "trend_vs_ma50",
                Decimal(f"{gap:.2f}"),
                "%",
                _pct(gap),
                f"{symbol} close versus 50-day average",
                ref,
            )
        )
    return facts


def add_price_sets(
    book: FactBook, conn: Connection, symbol: str, today: date, tz: ZoneInfo
) -> None:
    bars = load_daily_bars(conn, [symbol], today + timedelta(days=1), tz).get(symbol, [])
    for level in level_menu(symbol, bars):
        if level.name == "last_close":
            book.last_close = level.value
        book.add(Fact(level.id, level.value, "USD", f"${level.value:,}", level.label, level.ref))
    for fact in trend_facts(symbol, bars):
        book.add(fact)


def add_macro(book: FactBook, conn: Connection) -> None:
    rows = conn.execute(
        text(
            "SELECT DISTINCT ON (series_id, period) series_id, period, value "
            "FROM series_observations WHERE source = 'fred' AND series_id = ANY(:ids) "
            "AND value IS NOT NULL ORDER BY series_id, period, fetched_at DESC"
        ),
        {"ids": list(MACRO_SERIES)},
    ).all()
    series: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for row in rows:
        series[row.series_id].append((row.period, float(row.value)))
    for series_id, (label, unit) in MACRO_SERIES.items():
        values = sorted(series.get(series_id, []))
        if not values:
            continue
        day, latest = values[-1]
        ref = f"series_observations:fred:{series_id}:{day}"
        shown = f"{latest:.2f}%" if unit == "%" else f"{latest:.2f}"
        book.add(Fact(f"macro_{series_id}", Decimal(f"{latest:.4f}"), unit, shown, label, ref))
        if len(values) > MACRO_CHANGE_DAYS:
            start_day, start = values[-(MACRO_CHANGE_DAYS + 1)]
            if unit == "%":
                change, change_unit = (latest - start) * 100, "bp"
                text_value = f"{change:+.0f} bp"
            else:
                change, change_unit = (latest / start - 1) * 100, "%"
                text_value = _pct(change)
            book.add(
                Fact(
                    f"macro_{series_id}_chg",
                    Decimal(f"{change:.2f}"),
                    change_unit,
                    text_value,
                    f"{label} change over {MACRO_CHANGE_DAYS} observations",
                    f"series_observations:fred:{series_id}:{start_day}..{day}",
                )
            )


def add_cot(book: FactBook, conn: Connection, symbol: str) -> None:
    future = symbol if symbol in COT_INSTRUMENTS.values() else COT_PROXIES.get(symbol)
    code = next((c for c, inst in COT_INSTRUMENTS.items() if inst == future), None)
    if code is None:
        return
    rows = conn.execute(
        text(
            "SELECT DISTINCT ON (series_id, period) series_id, period, value "
            "FROM series_observations WHERE source = 'cftc_cot' AND series_id = ANY(:ids) "
            "ORDER BY series_id, period, fetched_at DESC"
        ),
        {"ids": [f"{code}.m_money_positions_long_all", f"{code}.m_money_positions_short_all"]},
    ).all()
    by_period: dict[date, dict[str, float]] = defaultdict(dict)
    for row in rows:
        by_period[row.period][row.series_id.split(".", 1)[1]] = float(row.value)
    net = sorted(
        (period, v["m_money_positions_long_all"] - v["m_money_positions_short_all"])
        for period, v in by_period.items()
        if len(v) == 2
    )
    if len(net) < 52:
        return
    period, latest = net[-1]
    rank = percentile_rank([value for _, value in net[-157:]], latest)
    ref = f"series_observations:cftc_cot:{code}.m_money:{period}"
    book.add(
        Fact(
            "cot_net",
            Decimal(int(latest)),
            "contracts",
            f"{latest:,.0f} contracts",
            f"{future} managed money net position",
            ref,
        )
    )
    book.add(
        Fact(
            "cot_pct",
            Decimal(f"{rank:.2f}"),
            "ratio",
            f"{rank:.0%}",
            f"{future} net position percentile over 3 years",
            ref,
        )
    )


def add_eia(book: FactBook, conn: Connection, symbol: str) -> None:
    future = symbol if symbol in EIA_INSTRUMENTS.values() else COT_PROXIES.get(symbol)
    index = 0
    for series_id, instrument in EIA_INSTRUMENTS.items():
        if instrument != future:
            continue
        rows = conn.execute(
            text(
                "SELECT DISTINCT ON (period) period, value FROM series_observations "
                "WHERE source = 'eia' AND series_id = :id ORDER BY period, fetched_at DESC"
            ),
            {"id": series_id},
        ).all()
        levels = {row.period: float(row.value) for row in rows}
        periods = sorted(levels)
        changes = {b: levels[b] - levels[a] for a, b in pairwise(periods) if (b - a).days <= 8}
        if not periods or periods[-1] not in changes:
            continue
        latest = periods[-1]
        past = [
            change
            for year in range(1, 6)
            for p, change in changes.items()
            if abs((p - (latest - timedelta(days=364 * year))).days) <= 3
        ]
        z = zscore(past, changes[latest])
        index += 1
        ref = f"series_observations:eia:{series_id}:{latest}"
        book.add(
            Fact(
                f"eia_{index}",
                Decimal(f"{changes[latest]:.1f}"),
                "level units",
                f"{changes[latest]:+,.1f}",
                f"EIA {series_id} weekly change",
                ref,
            )
        )
        if z is not None:
            index += 1
            book.add(
                Fact(
                    f"eia_{index}",
                    Decimal(f"{z:.2f}"),
                    "sigma",
                    f"{z:+.1f} sigma",
                    f"EIA {series_id} change versus the same week in 5 years",
                    ref,
                )
            )


def add_grid(book: FactBook, conn: Connection, now: datetime, tz: ZoneInfo) -> None:
    for index, (key, days) in enumerate(
        sorted(load_grid_peaks(conn, now - timedelta(days=30), tz).items()), start=1
    ):
        ordered = sorted(days)
        if len(ordered) < 2:
            continue
        latest_day, latest = ordered[-1]
        baseline = statistics.median(value for _, value in ordered[:-1])
        ref = f"grid_observations:{key}:{latest_day}"
        book.add(
            Fact(
                f"grid_{index}",
                Decimal(f"{latest:.2f}"),
                "$/MWh",
                f"${latest:,.2f}/MWh",
                f"{key} latest daily peak price",
                ref,
            )
        )
        if baseline > 0:
            book.add(
                Fact(
                    f"grid_{index}r",
                    Decimal(f"{latest / baseline:.2f}"),
                    "ratio",
                    f"{latest / baseline:.1f}x",
                    f"{key} peak versus its recent median peak",
                    ref,
                )
            )


def add_policy(book: FactBook, conn: Connection, now: datetime) -> None:
    rows = conn.execute(
        text(
            "SELECT id, payload->>'summary' AS summary FROM artifacts WHERE kind = 'trigger' "
            "AND payload->>'rule_id' = 'policy_keywords' AND created_at >= :since "
            "ORDER BY created_at DESC LIMIT :n"
        ),
        {"since": now - POLICY_WINDOW, "n": MAX_POLICY},
    ).all()
    for index, row in enumerate(rows, start=1):
        book.add(
            Fact(
                f"pol_{index}", row.summary, "", row.summary, "policy headline", f"trigger:{row.id}"
            )
        )


def build_book(
    conn: Connection,
    symbol: str,
    sets: set[str],
    now: datetime,
    tz: ZoneInfo,
    claims_since: datetime,
    evidence: tuple[UUID, ...] = (),
) -> FactBook:
    """Every fact set any caller needs for `symbol`; personas read subsets."""
    book = FactBook()
    today = now.astimezone(tz).date()
    add_claims(book, conn, symbol, claims_since, evidence)
    if sets & {"levels", "trend"}:
        add_price_sets(book, conn, symbol, today, tz)
    if "macro" in sets:
        add_macro(book, conn)
    if "cot" in sets:
        add_cot(book, conn, symbol)
    if "eia" in sets:
        add_eia(book, conn, symbol)
    if "grid" in sets:
        add_grid(book, conn, now, tz)
    if "policy" in sets:
        add_policy(book, conn, now)
    return book


def listing(book: FactBook) -> str:
    return book.table().prompt_listing() or "(no facts)"
