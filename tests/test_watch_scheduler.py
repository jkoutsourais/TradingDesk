from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Connection, text

from desk.artifacts.raw_record import RawRecord, content_hash
from desk.artifacts.store import append_artifact
from desk.collectors.base import CollectResult, SeriesObservation
from desk.collectors.ingest import ingest
from desk.watch import queue
from desk.watch.calendar import MarketCalendar, load_calendar_config
from desk.watch.rules import Hit, load_watch_config
from desk.watch.scan import TierMap, commodity_hits, emit, news_hits
from desk.watch.scheduler import Planner

NY = ZoneInfo("America/New_York")
CONFIG = load_watch_config()


@pytest.fixture(scope="module")
def calendar() -> MarketCalendar:
    return MarketCalendar(load_calendar_config())


def tier_map(**overrides: set[str]) -> TierMap:
    base = {
        "held": {"AEP"},
        "tier1": {"NVDA", "/GC", "/CL"},
        "universe": {"MMM", "NVDA"},
        "macro": {"^VIX"},
    }
    base.update(overrides)
    return TierMap(**base)


# --- Queue ------------------------------------------------------------------------------


def test_queue_dedupes_claims_once_and_retries(db_conn: Connection) -> None:
    key = f"test:{uuid4()}"
    first = queue.enqueue(db_conn, "scan", {"group": "news"}, dedupe_key=key, max_attempts=2)
    assert first is not None
    assert queue.enqueue(db_conn, "scan", {"group": "news"}, dedupe_key=key) is None

    job = queue.claim(db_conn)
    assert job is not None and job.id == first and job.attempts == 1
    assert queue.finish(db_conn, job, "boom") == "queued"  # one attempt left
    db_conn.execute(text("UPDATE jobs SET run_after = now() WHERE id = :id"), {"id": first})
    again = queue.claim(db_conn)
    assert again is not None and again.attempts == 2
    assert queue.finish(db_conn, again, "boom") == "failed"


# --- Planner -----------------------------------------------------------------------------


def test_shift_times_normal_half_day_and_sunday(calendar: MarketCalendar) -> None:
    planner = Planner(calendar, CONFIG)
    normal = dict(planner.shift_times(date(2026, 9, 23)))
    assert normal["pre_market"].astimezone(NY).hour == 7
    assert normal["briefing"].astimezone(NY).hour == 8
    assert normal["post_market"].astimezone(NY).hour == 17
    half_day = dict(planner.shift_times(date(2026, 11, 27)))  # closes 13:00
    assert half_day["post_market"].astimezone(NY).hour == 14
    assert planner.shift_times(date(2026, 11, 26)) == []  # Thanksgiving
    sunday = dict(planner.shift_times(date(2026, 9, 27)))
    assert list(sunday) == ["sunday_futures"]
    assert sunday["sunday_futures"].astimezone(NY).hour == 19


# --- Emission ----------------------------------------------------------------------------


def hit(instrument: str, fingerprint: str, rule: str = "move_vs_prior_close") -> Hit:
    return Hit(rule, instrument, f"{instrument} moved", fingerprint, excess=1.0)


def test_emit_scores_by_tier_and_applies_cooldown(
    db_conn: Connection, calendar: MarketCalendar
) -> None:
    now = datetime.now(UTC)
    fp = f"test:{uuid4()}"
    first = emit(db_conn, [hit("AEP", fp)], tier_map(), CONFIG, calendar, now)
    assert len(first.triggers) == 1
    trigger = first.triggers[0]
    assert trigger.tier == 0 and trigger.importance > 0
    again = emit(db_conn, [hit("AEP", fp)], tier_map(), CONFIG, calendar, now)
    assert again.triggers == [] and again.suppressed == 1


def test_urgent_flag_follows_threshold(db_conn: Connection, calendar: MarketCalendar) -> None:
    big = Hit("move_vs_prior_close", "AEP", "big", f"test:{uuid4()}", excess=10.0)
    small = Hit("move_vs_prior_close", "MMM", "small", f"test:{uuid4()}", excess=0.0)
    outcome = emit(db_conn, [big, small], tier_map(), CONFIG, calendar, datetime.now(UTC))
    urgent = {t.instrument: t.urgent for t in outcome.triggers}
    assert urgent == {"AEP": True, "MMM": False}


def test_event_addition_promotes_tier2_symbol_once(
    db_conn: Connection, calendar: MarketCalendar
) -> None:
    now = datetime.now(UTC)
    outcome = emit(
        db_conn, [hit("MMM", f"test:{uuid4()}", "volume_pace")], tier_map(), CONFIG, calendar, now
    )
    assert outcome.promotions == ["MMM"]
    repeat = emit(
        db_conn, [hit("MMM", f"test:{uuid4()}", "volume_pace")], tier_map(), CONFIG, calendar, now
    )
    assert repeat.promotions == []
    held = emit(
        db_conn, [hit("AEP", f"test:{uuid4()}", "volume_pace")], tier_map(), CONFIG, calendar, now
    )
    assert held.promotions == []  # already Tier 0


# --- News and filing rules ----------------------------------------------------------------


def record(source: str, source_id: str, payload: dict, tickers: tuple[str, ...] = ()) -> RawRecord:
    now = datetime.now(UTC)
    return RawRecord(
        produced_by="data.test",
        runtime_ms=0,
        source=source,
        source_id=source_id,
        fetched_at=now,
        tickers=tickers,
        payload=payload,
        content_hash=content_hash(source, source_id, str(uuid4())),
    )


def test_news_hits_filings_earnings_and_policy(db_conn: Connection) -> None:
    now = datetime.now(UTC)
    today = now.astimezone(NY).date()
    filing = record("edgar.filing", f"acc-{uuid4().hex[:8]}", {"form": "8-K"}, ("NVDA",))
    ignored_form = record("edgar.filing", f"acc-{uuid4().hex[:8]}", {"form": "D"}, ("NVDA",))
    earnings = record(
        "finnhub.earnings_calendar",
        f"AEP:{uuid4().hex[:4]}",
        {"symbol": "AEP", "date": today.isoformat(), "hour": "bmo"},
        ("AEP",),
    )
    post = record(
        "truth_social.post",
        f"post-{uuid4().hex[:8]}",
        {"content": "<p>New TARIFFS on steel starting Monday</p>"},
    )
    for r in (filing, ignored_form, earnings, post):
        append_artifact(db_conn, r)

    hits = news_hits(db_conn, tier_map(), CONFIG, now, today)
    by_rule = {(h.rule_id, h.instrument) for h in hits}
    assert ("filing", "NVDA") in by_rule
    assert ("earnings", "AEP") in by_rule
    assert ("policy_keywords", "policy:tariffs") in by_rule
    filing_hit = next(h for h in hits if h.rule_id == "filing" and h.instrument == "NVDA")
    assert filing_hit.parents == (filing.id,)
    assert not any(ignored_form.id in h.parents for h in hits)


def test_cot_extreme_and_eia_surprise(db_conn: Connection) -> None:
    fetched = datetime.now(UTC)
    start = date(2023, 9, 19)
    observations = []
    # Managed money net climbs steadily for 160 weeks: the latest week is the extreme.
    for week in range(160):
        period = start + timedelta(weeks=week)
        observations += [
            SeriesObservation(
                "cftc_cot",
                "088691.m_money_positions_long_all",
                period,
                Decimal(100_000 + week * 500),
                "contracts",
                fetched,
            ),
            SeriesObservation(
                "cftc_cot",
                "088691.m_money_positions_short_all",
                period,
                Decimal(20_000),
                "contracts",
                fetched,
            ),
        ]
    # Crude stocks: small weekly changes for five years, then a huge build.
    level = Decimal(400_000)
    eia_start = date(2021, 6, 4)
    for week in range(275):
        period = eia_start + timedelta(weeks=week)
        level += Decimal(20_000 if week == 274 else ((week * 37) % 9) * 100 - 400)
        observations.append(
            SeriesObservation("eia", "PET.WCESTUS1.W", period, level, "MBBL", fetched)
        )
    ingest(db_conn, CollectResult(observations=observations))

    hits = {h.rule_id: h for h in commodity_hits(db_conn, CONFIG)}
    assert hits["cot_extreme"].instrument == "/GC"
    assert "long" in hits["cot_extreme"].summary
    assert hits["eia_surprise"].instrument == "/CL"
    assert "build, above the seasonal norm" in hits["eia_surprise"].summary


def test_wide_quotes_do_not_count_as_prices() -> None:
    from desk.watch.scan import usable_mid

    assert usable_mid(Decimal("36.09"), Decimal("36.14")) == 36.115
    assert usable_mid(Decimal("71.65"), Decimal("73.37")) is None
    assert usable_mid(None, Decimal("1")) is None
    assert usable_mid(Decimal("0"), Decimal("1")) is None
