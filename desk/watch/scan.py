"""Scan groups for the Watch desk: load features, run rules, emit Trigger artifacts.

Groups (the scheduler decides when each runs):
  intraday   price rules on Tier 0-1 (every minute) or Tier 2 (every few minutes)
  close      52-week, IV-rank and relative-strength rules after the close
  news       filings, earnings and policy keywords on recently collected raw records
  commodity  CFTC positioning extremes and EIA inventory surprises

Emission applies each fingerprint's cooldown, scores importance, writes the Trigger and,
for event additions, promotes symbols outside the tiers into Tier 1 for a few days.
"""

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Any

from sqlalchemy import Connection, text

from desk.artifacts.store import append_artifact
from desk.artifacts.trigger import ObservedValue, Trigger
from desk.collectors.embeddings import record_text
from desk.collectors.holdings import held_symbols
from desk.config import TiersConfig, UniverseConfig
from desk.watch.calendar import MarketCalendar, MarketPhase
from desk.watch.rules import (
    CLOSE_RULES,
    INTRADAY_RULES,
    DailyBar,
    Features,
    Hit,
    WatchConfig,
    dec,
    importance,
    percentile_rank,
    relative_strength_hits,
    rule_move_vs_prior_close,
    rule_range_break,
    zscore,
)

DAILY_HISTORY_DAYS = 400
QUOTE_MAX_AGE = timedelta(minutes=15)
NEWS_LOOKBACK = timedelta(hours=2)
NEWS_COOLDOWN_MINUTES = 7 * 24 * 60
POLICY_SOURCES = (
    "truth_social.post",
    "fed.speech",
    "fed.press",
    "federal_register.document",
    "federal_register.public_inspection",
)
COT_INSTRUMENTS = {
    "088691": "/GC",
    "084691": "/SI",
    "085692": "/HG",
    "067651": "/CL",
    "023651": "/NG",
}
EIA_INSTRUMENTS = {
    "PET.WCESTUS1.W": "/CL",
    "PET.WGTSTUS1.W": "/CL",
    "PET.WDISTUS1.W": "/CL",
    "PET.W_EPC0_SAX_YCUOK_MBBL.W": "/CL",
    "NG.NW2_EPG0_SWO_R48_BCF.W": "/NG",
}
EXTENDED_HOURS_RULES = (rule_move_vs_prior_close, rule_range_break)


# --- Tiers --------------------------------------------------------------------------------


@dataclass
class TierMap:
    held: set[str]
    tier1: set[str]
    universe: set[str]
    macro: set[str]

    def tier(self, symbol: str) -> int | None:
        if symbol in self.held:
            return 0
        if symbol in self.tier1:
            return 1
        if symbol in self.universe:
            return 2
        if symbol in self.macro:
            return 3
        return None

    def symbols(self, *tiers: int) -> list[str]:
        groups = {
            0: self.held,
            1: self.tier1 - self.held,
            2: self.universe - self.tier1 - self.held,
        }
        return sorted({s for t in tiers for s in groups.get(t, set())})


def load_tier_map(
    conn: Connection, tiers: TiersConfig, universe: UniverseConfig, today: date
) -> TierMap:
    promoted = {
        row[0]
        for row in conn.execute(
            text("SELECT DISTINCT symbol FROM tier_promotions WHERE expires_on >= :today"),
            {"today": today},
        )
    }
    return TierMap(
        held=set(held_symbols(conn)),
        tier1=set(tiers.tier_1_symbols()) | promoted,
        universe=set(universe.symbols),
        macro=set(tiers.tier_3_macro.symbols),
    )


# --- Features -----------------------------------------------------------------------------


def load_daily_bars(
    conn: Connection, symbols: list[str], today: date, tz: Any
) -> dict[str, list[DailyBar]]:
    rows = conn.execute(
        text(
            "SELECT symbol, ts, open, high, low, close, volume FROM price_bars "
            "WHERE source = 'yahoo' AND interval = '1d' AND symbol = ANY(:symbols) "
            "AND ts >= :since ORDER BY symbol, ts"
        ),
        {"symbols": symbols, "since": datetime.now(UTC) - timedelta(days=DAILY_HISTORY_DAYS)},
    )
    history: dict[str, list[DailyBar]] = defaultdict(list)
    for row in rows:
        day = row.ts.astimezone(tz).date()
        if day >= today:
            continue
        history[row.symbol].append(
            DailyBar(
                day,
                float(row.open),
                float(row.high),
                float(row.low),
                float(row.close),
                float(row.volume) if row.volume is not None else None,
            )
        )
    return history


def load_session_bars(
    conn: Connection, symbols: list[str], session: Any, min_minutes: int = 300
) -> dict[str, DailyBar]:
    """Today's regular-session bar built from stored 1-minute bars.

    Yahoo's daily bar for today is only stored once the day is over, so the after-close
    scan assembles today's OHLCV from the stream instead. Symbols with thin minute
    coverage are left out rather than given a partial bar.
    """
    rows = conn.execute(
        text(
            "SELECT symbol, count(*) AS minutes, "
            "(array_agg(open ORDER BY ts))[1] AS open, max(high) AS high, min(low) AS low, "
            "(array_agg(close ORDER BY ts DESC))[1] AS close, sum(volume) AS volume "
            "FROM price_bars WHERE source = 'tastytrade' AND interval = '1m' "
            "AND symbol = ANY(:s) AND ts >= :open AND ts < :close GROUP BY symbol"
        ),
        {"s": symbols, "open": session.open, "close": session.close},
    )
    session_minutes = (session.close - session.open).total_seconds() / 60
    needed = min(min_minutes, session_minutes * 0.75)
    return {
        row.symbol: DailyBar(
            session.day,
            float(row.open),
            float(row.high),
            float(row.low),
            float(row.close),
            float(row.volume) if row.volume is not None else None,
        )
        for row in rows
        if row.minutes >= needed
    }


def load_features(
    conn: Connection,
    symbols: list[str],
    calendar: MarketCalendar,
    now: datetime,
    daily: dict[str, list[DailyBar]],
) -> list[Features]:
    today = now.astimezone(calendar.tz).date()
    session = calendar.session(today)
    quotes = {
        row.symbol: row
        for row in conn.execute(
            text(
                "SELECT symbol, bid, ask, quote_time, day_open, day_volume, day_stats_time "
                "FROM quotes_latest WHERE symbol = ANY(:s)"
            ),
            {"s": symbols},
        )
    }
    intraday: dict[str, Any] = {}
    if session is not None:
        for row in conn.execute(
            text(
                "SELECT symbol, sum(volume) AS volume, "
                "(array_agg(open ORDER BY ts))[1] AS first_open, min(ts) AS first_ts "
                "FROM price_bars WHERE source = 'tastytrade' AND interval = '1m' "
                "AND symbol = ANY(:s) AND ts >= :open AND ts < :close GROUP BY symbol"
            ),
            {"s": symbols, "open": session.open, "close": session.close},
        ):
            intraday[row.symbol] = row
    iv = {
        row.symbol: row
        for row in conn.execute(
            text(
                "SELECT DISTINCT ON (series_id) split_part(series_id, '.tw_', 1) AS symbol, "
                "series_id, period, value FROM series_observations "
                "WHERE source = 'tastytrade_metrics' "
                "AND series_id = ANY(:ids) ORDER BY series_id, period DESC, fetched_at DESC"
            ),
            {"ids": [f"{s}.tw_implied_volatility_index_rank" for s in symbols]},
        )
    }

    features = []
    for symbol in symbols:
        bars = daily.get(symbol)
        if not bars:
            continue
        f = Features(symbol=symbol, bars=bars, daily_ref=f"price_bars:yahoo:{symbol}:1d")
        quote = quotes.get(symbol)
        if (
            quote is not None
            and quote.bid is not None
            and quote.ask is not None
            and quote.quote_time is not None
            and now - quote.quote_time <= QUOTE_MAX_AGE
        ):
            f.last = (float(quote.bid) + float(quote.ask)) / 2
            f.last_ref = f"quotes_latest:{symbol}:{quote.quote_time.isoformat()}"
        if session is not None:
            f.session_minutes = (session.close - session.open).total_seconds() / 60
            if session.open <= now:
                f.minutes_since_open = min(
                    (now - session.open).total_seconds() / 60, f.session_minutes
                )
            bar = intraday.get(symbol)
            stats_fresh = (
                quote is not None
                and quote.day_stats_time is not None
                and quote.day_stats_time >= session.open
            )
            if bar is None and stats_fresh:
                # Symbols streamed without minute candles (Tier 2) carry the session's
                # volume and open from DXLink Trade and Summary events instead.
                stats_ref = f"quotes_latest:{symbol}:day_stats:{quote.day_stats_time.isoformat()}"
                if quote.day_volume is not None:
                    f.day_volume = float(quote.day_volume)
                    f.extra["volume_ref"] = stats_ref
                if quote.day_open is not None:
                    f.day_open = float(quote.day_open)
                    f.extra["open_ref"] = stats_ref
            if bar is not None:
                f.day_volume = float(bar.volume) if bar.volume is not None else None
                f.extra["volume_ref"] = (
                    f"price_bars:tastytrade:{symbol}:1m:{session.open.isoformat()}..{now.isoformat()}"
                )
                # A gap is measured only when the first bar is at the opening minute.
                if bar.first_ts - session.open <= timedelta(minutes=2):
                    f.day_open = float(bar.first_open)
                    f.extra["open_ref"] = (
                        f"price_bars:tastytrade:{symbol}:1m:{bar.first_ts.isoformat()}"
                    )
        metric = iv.get(symbol)
        if metric is not None:
            f.iv_rank = float(metric.value)
            f.iv_ref = f"series_observations:tastytrade_metrics:{metric.series_id}:{metric.period}"
        features.append(f)
    return features


# --- Emission -----------------------------------------------------------------------------


@dataclass
class ScanOutcome:
    hits: int = 0
    triggers: list[Trigger] = field(default_factory=list)
    suppressed: int = 0
    promotions: list[str] = field(default_factory=list)


def _in_cooldown(conn: Connection, fingerprint: str, minutes: int) -> bool:
    return conn.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM artifacts WHERE kind = 'trigger' "
            "AND payload->>'fingerprint' = :fp AND created_at >= :since)"
        ),
        {"fp": fingerprint, "since": datetime.now(UTC) - timedelta(minutes=minutes)},
    ).scalar_one()


def _promote(
    conn: Connection, trigger: Trigger, calendar: MarketCalendar, config: WatchConfig, today: date
) -> bool:
    additions = config.event_additions
    already = conn.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM tier_promotions WHERE symbol = :s AND expires_on >= :d)"
        ),
        {"s": trigger.instrument, "d": today},
    ).scalar_one()
    promoted_today = conn.execute(
        text("SELECT count(*) FROM tier_promotions WHERE promoted_at >= :start"),
        {"start": datetime.combine(today, datetime.min.time(), calendar.tz)},
    ).scalar_one()
    if already or promoted_today >= additions.max_per_day:
        return False
    conn.execute(
        text(
            "INSERT INTO tier_promotions (symbol, expires_on, reason, trigger_id) "
            "VALUES (:s, :expires, :reason, :trigger)"
        ),
        {
            "s": trigger.instrument,
            "expires": calendar.add_trading_days(today, additions.trading_days),
            "reason": trigger.summary,
            "trigger": trigger.id,
        },
    )
    return True


def emit(
    conn: Connection,
    hits: Iterable[Hit],
    tier_map: TierMap,
    config: WatchConfig,
    calendar: MarketCalendar,
    now: datetime,
    shift_id: Any = None,
) -> ScanOutcome:
    outcome = ScanOutcome()
    today = now.astimezone(calendar.tz).date()
    for hit in hits:
        outcome.hits += 1
        rule = config.rule(hit.rule_id)
        cooldown = hit.cooldown_minutes or int(
            rule.get("cooldown_minutes", config.default_cooldown_minutes)
        )
        if _in_cooldown(conn, hit.fingerprint, cooldown):
            outcome.suppressed += 1
            continue
        tier = tier_map.tier(hit.instrument)
        score = importance(hit, tier, config)
        trigger = Trigger(
            produced_by=f"watch.{hit.rule_id}",
            runtime_ms=0,
            shift_id=shift_id,
            parents=hit.parents,
            rule_id=hit.rule_id,
            instrument=hit.instrument,
            tier=tier,
            importance=score,
            urgent=score >= config.urgent_threshold,
            summary=hit.summary,
            fingerprint=hit.fingerprint,
            observed=hit.observed,
        )
        append_artifact(conn, trigger)
        outcome.triggers.append(trigger)
        if (
            hit.rule_id in config.event_additions.rules
            and tier not in (0, 1)
            and not hit.instrument.startswith("policy:")
            and _promote(conn, trigger, calendar, config, today)
        ):
            outcome.promotions.append(hit.instrument)
    return outcome


# --- Scan groups --------------------------------------------------------------------------


def intraday_hits(
    features: list[Features],
    tier_map: TierMap,
    config: WatchConfig,
    phase: MarketPhase,
    today: date,
) -> list[Hit]:
    rules = INTRADAY_RULES if phase is MarketPhase.REGULAR else EXTENDED_HOURS_RULES
    hits = []
    for f in features:
        tier = tier_map.tier(f.symbol)
        for rule in rules:
            hit = rule(f, tier if tier is not None else 2, config, today)
            if hit is not None:
                hits.append(hit)
    return hits


def close_hits(
    features: list[Features], tier_map: TierMap, config: WatchConfig, today: date
) -> list[Hit]:
    hits = []
    for f in features:
        tier = tier_map.tier(f.symbol)
        for rule in CLOSE_RULES:
            hit = rule(f, tier if tier is not None else 2, config, today)
            if hit is not None:
                hits.append(hit)
    return hits + relative_strength_hits(features, config, today)


def _recent_records(conn: Connection, sources: list[str], since: datetime) -> list[Any]:
    return list(
        conn.execute(
            text(
                "SELECT id, created_at, payload FROM artifacts WHERE kind = 'raw_record' "
                "AND payload->>'source' = ANY(:sources) AND created_at >= :since "
                "ORDER BY created_at"
            ),
            {"sources": sources, "since": since},
        )
    )


def news_hits(
    conn: Connection, tier_map: TierMap, config: WatchConfig, now: datetime, today: date
) -> list[Hit]:
    since = now - NEWS_LOOKBACK
    hits: list[Hit] = []
    watched = tier_map.held | tier_map.tier1 | tier_map.universe

    filing_rule = config.rule("filing")
    forms = set(filing_rule["forms"])
    for row in _recent_records(conn, ["edgar.filing"], since):
        form = row.payload["payload"].get("form", "")
        if form not in forms:
            continue
        for ticker in row.payload.get("tickers", []):
            if ticker in watched:
                hits.append(
                    Hit(
                        rule_id="filing",
                        instrument=ticker,
                        summary=f"{ticker} filed a {form}",
                        fingerprint=f"filing:{row.payload['source_id']}:{ticker}",
                        excess=None,
                        parents=(row.id,),
                        cooldown_minutes=NEWS_COOLDOWN_MINUTES,
                    )
                )

    cluster = int(filing_rule["form4_cluster"])
    for row in conn.execute(
        text(
            "SELECT ticker, count(*) AS n, array_agg(id) AS ids FROM ("
            "  SELECT id, jsonb_array_elements_text(payload->'tickers') AS ticker "
            "  FROM artifacts WHERE kind = 'raw_record' AND payload->>'source' = 'edgar.filing' "
            "  AND payload->'payload'->>'form' = '4' AND created_at >= :since"
            ") t WHERE ticker = ANY(:watched) GROUP BY ticker HAVING count(*) >= :cluster"
        ),
        {
            "since": now - timedelta(days=5),
            "watched": sorted(tier_map.held | tier_map.tier1),
            "cluster": cluster,
        },
    ):
        hits.append(
            Hit(
                rule_id="filing",
                instrument=row.ticker,
                summary=f"{row.ticker}: {row.n} insider Form 4 filings in 5 days",
                fingerprint=f"form4_cluster:{row.ticker}:{today.isoformat()}",
                excess=None,
                observed=(
                    ObservedValue(
                        name="form4_count_5d",
                        value=dec(row.n),
                        unit="filings",
                        source_ref="raw_records:edgar.filing:form4:5d",
                    ),
                ),
                parents=tuple(row.ids[:20]),
                cooldown_minutes=NEWS_COOLDOWN_MINUTES,
            )
        )

    days_ahead = int(config.rule("earnings")["days_ahead"])
    horizon = {(today + timedelta(days=offset)).isoformat() for offset in range(days_ahead + 1)}
    seen: set[str] = set()
    for row in _recent_records(conn, ["finnhub.earnings_calendar"], now - timedelta(days=2)):
        entry = row.payload["payload"]
        symbol = entry.get("symbol", "")
        when = entry.get("date", "")
        if when in horizon and symbol in watched and symbol not in seen:
            seen.add(symbol)
            timing = {"bmo": "before the open", "amc": "after the close"}.get(entry.get("hour"), "")
            hits.append(
                Hit(
                    rule_id="earnings",
                    instrument=symbol,
                    summary=f"{symbol} reports earnings {when} {timing}".strip(),
                    fingerprint=f"earnings:{symbol}:{when}",
                    excess=None,
                    parents=(row.id,),
                    cooldown_minutes=NEWS_COOLDOWN_MINUTES,
                )
            )

    groups = config.policy_groups()
    patterns = {
        name: re.compile(
            r"(?<![\w+])(" + "|".join(re.escape(k) for k in group.keywords) + r")(?![\w+])",
            re.IGNORECASE,
        )
        for name, group in groups.items()
    }
    for row in _recent_records(conn, list(POLICY_SOURCES), since):
        body = record_text(row.payload["source"], row.payload["payload"], row.payload.get("url"))
        for name, pattern in patterns.items():
            match = pattern.search(body)
            if match is None:
                continue
            source = row.payload["source"].split(".")[0]
            hits.append(
                Hit(
                    rule_id="policy_keywords",
                    instrument=f"policy:{name}",
                    summary=f"{source}: '{match.group(0)}' in {body[:120]}",
                    fingerprint=f"policy_keywords:{name}:{row.id}",
                    excess=None,
                    parents=(row.id,),
                    cooldown_minutes=NEWS_COOLDOWN_MINUTES,
                )
            )
    return hits


def commodity_hits(conn: Connection, config: WatchConfig) -> list[Hit]:
    hits: list[Hit] = []
    cot = config.rule("cot_extreme")
    level = float(cot["percentile"])
    for code, instrument in COT_INSTRUMENTS.items():
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
            continue  # not enough history for a meaningful percentile
        period, latest = net[-1]
        rank = percentile_rank([value for _, value in net[-157:]], latest)
        if level > rank > 1 - level:
            continue
        side = "long" if rank >= level else "short"
        hits.append(
            Hit(
                rule_id="cot_extreme",
                instrument=instrument,
                summary=(
                    f"{instrument} managed money net {side} at the "
                    f"{rank:.0%} percentile of ~3 years"
                ),
                fingerprint=f"cot_extreme:{code}:{period.isoformat()}",
                excess=None,
                observed=(
                    ObservedValue(
                        name="managed_money_net",
                        value=dec(latest),
                        unit="contracts",
                        source_ref=f"series_observations:cftc_cot:{code}.m_money:{period}",
                    ),
                    ObservedValue(
                        name="percentile_3y",
                        value=dec(rank),
                        unit="ratio",
                        source_ref=f"computed:percentile(cftc_cot:{code}.m_money_net,156w)",
                    ),
                ),
                cooldown_minutes=NEWS_COOLDOWN_MINUTES,
            )
        )

    sigma = float(config.rule("eia_surprise")["sigma"])
    for series_id, instrument in EIA_INSTRUMENTS.items():
        rows = conn.execute(
            text(
                "SELECT DISTINCT ON (period) period, value FROM series_observations "
                "WHERE source = 'eia' AND series_id = :id ORDER BY period, fetched_at DESC"
            ),
            {"id": series_id},
        ).all()
        levels = {row.period: float(row.value) for row in rows}
        periods = sorted(levels)
        if len(periods) < 2:
            continue
        changes = {b: levels[b] - levels[a] for a, b in pairwise(periods) if (b - a).days <= 8}
        latest = periods[-1]
        if latest not in changes:
            continue
        past = [
            change
            for year in range(1, 6)
            for p, change in changes.items()
            if abs((p - (latest - timedelta(days=364 * year))).days) <= 3
        ]
        z = zscore(past, changes[latest])
        if z is None or abs(z) < sigma:
            continue
        # Describe the change against the seasonal norm: a positive z is more inventory
        # than usual for the week (bigger build or smaller draw), a negative z less.
        change_word = "build" if changes[latest] > 0 else "draw"
        if z > 0:
            direction = f"{change_word}, above the seasonal norm"
        else:
            direction = f"{change_word}, below the seasonal norm"
        hits.append(
            Hit(
                rule_id="eia_surprise",
                instrument=instrument,
                summary=(
                    f"EIA {series_id}: weekly {direction} "
                    f"({z:+.1f} sigma vs the same week in 5 years)"
                ),
                fingerprint=f"eia_surprise:{series_id}:{latest.isoformat()}",
                excess=abs(z) - sigma,
                observed=(
                    ObservedValue(
                        name="weekly_change",
                        value=dec(changes[latest]),
                        unit="level units",
                        source_ref=f"series_observations:eia:{series_id}:{latest}",
                    ),
                    ObservedValue(
                        name="seasonal_z",
                        value=dec(z),
                        unit="sigma",
                        source_ref=f"computed:zscore(eia:{series_id},same week 5y)",
                    ),
                ),
                cooldown_minutes=NEWS_COOLDOWN_MINUTES,
            )
        )
    return hits
