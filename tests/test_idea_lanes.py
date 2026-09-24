from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

from desk.artifacts.idea import LaneCandidate
from desk.config import load_tiers
from desk.desks.idea.lanes import (
    Signal,
    TriggerRow,
    combine,
    fred_signals,
    grid_signals,
    group_of,
    load_lanes_config,
    route,
    trigger_signals,
)
from desk.desks.idea.levels import level_menu
from desk.desks.idea.select import finalize, shortlist
from desk.watch.rules import DailyBar, load_watch_config
from desk.watch.scan import TierMap

CONFIG = load_lanes_config()
TIERS = load_tiers()
GROUPS = group_of(TIERS)
TIER_MAP = TierMap(held={"AEP"}, tier1=set(TIERS.tier_1_symbols()), universe={"DXCM"}, macro=set())


def trig(rule: str, instrument: str, importance: float = 0.5) -> TriggerRow:
    return TriggerRow(uuid4(), rule, instrument, importance, f"{instrument} {rule}")


def lane_of(rule: str, instrument: str) -> str | None:
    return route(trig(rule, instrument), TIER_MAP.tier(instrument), GROUPS, CONFIG)


def test_routing_by_rule_group_and_tier() -> None:
    assert lane_of("range_break", "NVDA") == "screen"
    assert lane_of("filing", "NVDA") == "catalyst"
    assert lane_of("range_break", "GLD") == "commodity"
    assert lane_of("move_vs_prior_close", "/CL") == "commodity"
    assert lane_of("eia_surprise", "/NG") == "commodity"
    assert lane_of("range_break", "VST") == "power_grid"
    assert lane_of("filing", "DXCM") == "event_additions"
    assert lane_of("range_break", "DXCM") == "screen"
    assert lane_of("policy_keywords", "policy:tariffs") == "policy"
    assert lane_of("move_vs_prior_close", "^TNX") == "macro"


def test_policy_and_macro_expand_to_instruments() -> None:
    policy_groups = load_watch_config().policy_groups()
    signals = trigger_signals(
        [trig("policy_keywords", "policy:opec"), trig("move_vs_prior_close", "DX-Y.NYB")],
        TIER_MAP,
        GROUPS,
        policy_groups,
        CONFIG,
    )
    by_lane = {(s.lane, s.instrument) for s in signals}
    assert ("policy", "/CL") in by_lane and ("policy", "XLE") in by_lane
    assert ("macro", "UUP") in by_lane and ("macro", "GLD") in by_lane


def test_combine_scores_with_weight_and_claim_bonus() -> None:
    parent = uuid4()
    signals = [
        Signal("screen", "NVDA", 0.5, "NVDA broke out", f"trigger:{parent}", parent),
        Signal("screen", "NVDA", 0.5, "NVDA gapped", "trigger:x", None),
        Signal("catalyst", "MSFT", 0.4, "MSFT 8-K", "trigger:y", None),
    ]
    candidates = combine(signals, {"NVDA": 10}, CONFIG)
    nvda = next(c for c in candidates if c.instrument == "NVDA")
    # 0.9 x (1 - 0.5 x 0.5) + min(10 x 0.05, 0.15)
    assert nvda.score == 0.825
    assert nvda.parents == (parent,)
    assert any(i.name.startswith("verified claims") for i in nvda.ingredients)
    msft = next(c for c in candidates if c.instrument == "MSFT")
    assert msft.score == 0.4
    assert candidates[0].instrument == "NVDA"


def test_fred_screen_maps_real_yield_drop_to_gold() -> None:
    start = date(2026, 8, 1)
    values = [(start + timedelta(days=i), 2.0 - i * 0.02) for i in range(21)]
    signals = fred_signals({"DFII10": values}, CONFIG)
    assert {s.instrument for s in signals} == {"GLD", "SLV"}
    assert "down -40 bp" in signals[0].driver
    assert signals[0].importance > 0.5
    flat = [(start + timedelta(days=i), 2.0) for i in range(21)]
    assert fred_signals({"DFII10": flat}, CONFIG) == []


def test_grid_screen_needs_history_and_a_spike() -> None:
    start = date(2026, 9, 1)
    calm = [(start + timedelta(days=i), 50.0) for i in range(7)]
    spike = [*calm, (start + timedelta(days=7), 150.0)]
    assert grid_signals({"ercot:price.hb_houston": calm[:3]}, CONFIG) == []
    assert grid_signals({"ercot:price.hb_houston": calm}, CONFIG) == []
    signals = grid_signals({"ercot:price.hb_houston": spike}, CONFIG)
    assert {s.instrument for s in signals} == {"VST", "NRG", "TLN"}


def candidate(instrument: str, score: float, lane: str = "screen") -> LaneCandidate:
    return LaneCandidate(
        produced_by="idea.lanes",
        runtime_ms=0,
        lane=lane,
        instrument=instrument,
        driver=f"{instrument} driver",
        score=score,
        ingredients=[{"name": "importance", "value": score, "source_ref": "t"}],
    )


def test_shortlist_drops_held_duplicates_and_cut() -> None:
    items = [
        candidate("AEP", 0.9),
        candidate("NVDA", 0.8),
        candidate("NVDA", 0.7, "catalyst"),
        candidate("GLD", 0.6, "commodity"),
        candidate("XOM", 0.5, "commodity"),
    ]
    listed = shortlist(items, held={"AEP"}, limit=2)
    assert [c.instrument for c in listed.chosen] == ["NVDA", "GLD"]
    reasons = {c.instrument + c.lane: listed.dropped.get(c.id) for c in items}
    assert reasons["AEPscreen"].startswith("held")
    assert reasons["NVDAcatalyst"] == "duplicate of NVDA (screen)"
    assert reasons["XOMcommodity"] == "below the research cut"
    covered = shortlist(items, held=set(), limit=5, covered=frozenset({"GLD"}))
    assert covered.dropped[items[3].id] == "an open thesis already covers it"


def test_finalize_keeps_evidence_backed_candidates() -> None:
    items = [candidate("NVDA", 0.8), candidate("GLD", 0.6), candidate("XOM", 0.5)]
    listed = shortlist(items, held=set(), limit=3)
    selection = finalize(items, listed, {"GLD": 2, "XOM": 1}, keep=1)
    assert selection.selected == (items[1].id,)
    reasons = {d.candidate_id: d.reason for d in selection.dropped}
    assert reasons[items[0].id] == "no verified evidence from research"
    assert reasons[items[2].id] == "below the thesis cut"


def bars(count: int, close: float = 100.0) -> list[DailyBar]:
    start = date(2025, 1, 1)
    return [
        DailyBar(start + timedelta(days=i), close, close + 2, close - 2, close + (i % 3), 1e6)
        for i in range(count)
    ]


def test_level_menu() -> None:
    menu = level_menu("SLV", bars(300))
    names = {level.name: level for level in menu}
    assert {"last_close", "low_20d", "high_20d", "low_52w", "close_minus_2atr"} <= set(names)
    assert names["last_close"].value == Decimal("102.00")
    assert names["low_20d"].value == Decimal("98.00")
    assert names["close_minus_1atr"].value < names["last_close"].value
    assert names["low_20d"].ref.startswith("levels:SLV:low_20d:")
    assert [level.id for level in menu[:2]] == ["lvl_1", "lvl_2"]
    assert level_menu("SLV", []) == []
    short = {level.name for level in level_menu("SLV", bars(5))}
    assert short == {"last_close"}
