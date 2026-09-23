import math
from datetime import date, timedelta
from decimal import Decimal

import pytest

from desk.watch.rules import (
    DailyBar,
    Features,
    Hit,
    importance,
    load_watch_config,
    percentile_rank,
    relative_strength_hits,
    rule_gap_open,
    rule_move_vs_prior_close,
    rule_new_52w,
    rule_range_break,
    rule_volume_pace,
    strength,
    zscore,
)

TODAY = date(2026, 9, 23)
CONFIG = load_watch_config()


def bars(
    count: int, close: float = 100.0, spread: float = 1.0, volume: float = 1_000_000
) -> list[DailyBar]:
    """Flat history: every day trades close +/- spread/2, so ATR is exactly `spread`."""
    start = TODAY - timedelta(days=count)
    return [
        DailyBar(
            start + timedelta(days=i), close, close + spread / 2, close - spread / 2, close, volume
        )
        for i in range(count)
    ]


def features(**kwargs: object) -> Features:
    base: dict[str, object] = {
        "symbol": "TEST",
        "bars": bars(30),
        "daily_ref": "price_bars:yahoo:TEST:1d",
        "last_ref": "quotes_latest:TEST",
    }
    base.update(kwargs)
    return Features(**base)  # type: ignore[arg-type]


def test_strength_curve() -> None:
    assert strength(0.0, 1.0) == 0.5
    assert strength(None, 1.0) == 0.75
    assert strength(1.0, 1.0) == pytest.approx(0.5 + 0.5 * (1 - math.exp(-1)))
    assert strength(100.0, 1.0) == pytest.approx(1.0, abs=1e-6)


def test_importance_combines_rule_tier_and_strength() -> None:
    hit = Hit("move_vs_prior_close", "X", "s", "f", excess=0.0)
    assert importance(hit, 0, CONFIG) == pytest.approx(0.9 * 1.0 * 0.5)
    assert importance(hit, 2, CONFIG) == pytest.approx(0.9 * 0.55 * 0.5)
    assert importance(hit, 2, CONFIG) < importance(hit, 0, CONFIG)


def test_move_vs_prior_close_uses_tier_threshold() -> None:
    f = features(last=102.2)  # +2.2 ATR
    assert rule_move_vs_prior_close(f, 0, CONFIG, TODAY) is not None  # threshold 1.5
    assert rule_move_vs_prior_close(f, 1, CONFIG, TODAY) is not None  # threshold 2.0
    assert rule_move_vs_prior_close(f, 2, CONFIG, TODAY) is None  # threshold 2.5
    hit = rule_move_vs_prior_close(f, 1, CONFIG, TODAY)
    assert hit is not None
    assert hit.fingerprint == "move_vs_prior_close:TEST:2026-09-23:up"
    assert hit.excess == pytest.approx(0.2)
    values = {o.name: o.value for o in hit.observed}
    assert values["atr_multiple"] == Decimal("2.2000")
    assert all(o.source_ref for o in hit.observed)


def test_move_needs_enough_history() -> None:
    assert rule_move_vs_prior_close(features(bars=bars(10), last=150.0), 0, CONFIG, TODAY) is None


def test_gap_open_only_early_in_session() -> None:
    early = features(day_open=98.5, minutes_since_open=5)
    late = features(day_open=98.5, minutes_since_open=90)
    hit = rule_gap_open(early, 1, CONFIG, TODAY)
    assert hit is not None and "down" in hit.summary
    assert rule_gap_open(late, 1, CONFIG, TODAY) is None


def test_range_break_both_directions() -> None:
    up = rule_range_break(features(last=101.0), 1, CONFIG, TODAY)
    down = rule_range_break(features(last=99.0), 1, CONFIG, TODAY)
    inside = rule_range_break(features(last=100.2), 1, CONFIG, TODAY)
    assert up is not None and up.fingerprint.endswith(":above")
    assert down is not None and down.fingerprint.endswith(":below")
    assert inside is None


def test_volume_pace_prorates_by_time_of_day() -> None:
    # Half the session gone and already 1.5x a full day's volume: pace 3.0.
    hot = features(day_volume=1_500_000, minutes_since_open=195)
    normal = features(day_volume=500_000, minutes_since_open=195)
    too_early = features(day_volume=5_000_000, minutes_since_open=10)
    hit = rule_volume_pace(hot, 1, CONFIG, TODAY)
    assert hit is not None and hit.excess == pytest.approx(0.5)
    assert rule_volume_pace(normal, 1, CONFIG, TODAY) is None
    assert rule_volume_pace(too_early, 1, CONFIG, TODAY) is None


def test_new_52w_high() -> None:
    history = bars(260)
    last = history[-1]
    history[-1] = DailyBar(last.day, 104, 106, 103, 105, 1)
    hit = rule_new_52w(features(bars=history), 1, CONFIG, TODAY)
    assert hit is not None and hit.fingerprint.endswith(":high")


def test_relative_strength_picks_both_ends() -> None:
    universe = []
    for i in range(100):
        history = bars(25)
        end = history[-1]
        history[-1] = DailyBar(end.day, 100, 100 + i, 90, 100 + i - 50, 1)
        universe.append(features(symbol=f"S{i:03d}", bars=history))
    hits = relative_strength_hits(universe, CONFIG, TODAY)
    sides = {h.instrument: h.fingerprint.rsplit(":", 1)[1] for h in hits}
    assert sides["S099"] == "leader"
    assert sides["S000"] == "laggard"
    assert len(hits) == 6  # 3% of 100 on each side


def test_percentile_and_zscore() -> None:
    assert percentile_rank([1, 2, 3, 4], 4) == 1.0
    assert percentile_rank([1, 2, 3, 4], 1) == 0.25
    assert zscore([1.0, 2.0, 3.0], 3.0) == pytest.approx(1.0)
    assert zscore([1.0, 1.0, 1.0], 5.0) is None
