from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from desk.scoring.positions import FillRow, build_positions
from desk.scoring.rollup import ScoreRow, rollup
from desk.scoring.shadow import (
    adherence,
    rating_hit,
    score_path,
    stance_hit,
    verdict_hit,
)
from desk.watch.rules import DailyBar

START = date(2026, 9, 1)


def bars(*rows: tuple[float, float, float]) -> list[DailyBar]:
    """(low, high, close) per trading day after entry."""
    return [
        DailyBar(START + timedelta(days=i + 1), close, high, low, close, None)
        for i, (low, high, close) in enumerate(rows)
    ]


def test_not_scored_before_the_horizon() -> None:
    assert score_path("long", Decimal(100), None, None, bars((99, 101, 100)), 5) is None


def test_long_return_and_excursions() -> None:
    path = bars((98, 103, 102), (97, 105, 104), (99, 106, 101))
    result = score_path("long", Decimal(100), None, None, path, 3)
    assert result is not None
    assert result.return_pct == pytest.approx(1.0)
    assert result.mae_pct == pytest.approx(-3.0)
    assert result.mfe_pct == pytest.approx(6.0)
    assert result.first_hit == "none" and result.r_multiple is None


def test_short_is_direction_adjusted() -> None:
    result = score_path("short", Decimal(100), None, None, bars((90, 101, 95)), 1)
    assert result is not None
    assert result.return_pct == pytest.approx(5.0)
    assert result.mae_pct == pytest.approx(-1.0)
    assert result.mfe_pct == pytest.approx(10.0)


def test_stop_first_is_minus_one_r() -> None:
    path = bars((96, 101, 97), (94, 112, 110))
    result = score_path("long", Decimal(100), Decimal(95), Decimal(110), path, 2)
    assert result is not None
    assert result.first_hit == "stop" and result.r_multiple == pytest.approx(-1.0)


def test_target_first_and_same_bar_counts_as_stop() -> None:
    target = score_path("long", Decimal(100), Decimal(95), Decimal(110), bars((99, 111, 109)), 1)
    assert target is not None and target.first_hit == "target"
    assert target.r_multiple == pytest.approx(2.0)
    both = score_path("long", Decimal(100), Decimal(95), Decimal(110), bars((94, 111, 100)), 1)
    assert both is not None and both.first_hit == "stop"


def test_open_r_from_the_close_when_nothing_hit() -> None:
    result = score_path("long", Decimal(100), Decimal(95), Decimal(120), bars((97, 104, 102.5)), 1)
    assert result is not None and result.r_multiple == pytest.approx(0.5)
    short = score_path("short", Decimal(100), Decimal(104), Decimal(90), bars((97, 103, 98)), 1)
    assert short is not None and short.r_multiple == pytest.approx(0.5)


def test_hits_for_ratings_views_and_verdicts() -> None:
    assert rating_hit("buy_add", 2.0) is True and rating_hit("hold", -1.0) is False
    assert rating_hit("sell", -3.0) is True and rating_hit("trim", 1.0) is False
    assert stance_hit("for", 1.5) is True and stance_hit("against", 1.5) is False
    assert stance_hit("neutral", 1.5) is None
    assert verdict_hit("pursue", 0.5) is True and verdict_hit("reject", 0.5) is False
    assert verdict_hit("reject", -0.5) is True and verdict_hit("watch", 3.0) is None


def test_rollup_groups_by_dimension() -> None:
    rows = [
        ScoreRow({"lane": "screen", "persona": "technician"}, 2.0, 1.0, True),
        ScoreRow({"lane": "screen", "persona": "macro"}, -1.0, -1.0, False),
        ScoreRow({"lane": "commodity", "persona": "technician"}, 4.0, None, True),
    ]
    by_lane = {g.value: g for g in rollup(rows, "lane")}
    assert by_lane["screen"].count == 2 and by_lane["screen"].hit_rate == 0.5
    assert by_lane["screen"].avg_return == pytest.approx(0.5)
    assert by_lane["screen"].avg_r == pytest.approx(0.0)
    assert by_lane["commodity"].avg_r is None
    by_persona = {g.value: g for g in rollup(rows, "persona")}
    assert by_persona["technician"].count == 2 and by_persona["technician"].hit_rate == 1.0


def fill(
    side: str, qty: str, price: str, day: int, fee: str = "1", exec_id: str | None = None
) -> FillRow:
    return FillRow(
        exec_id=exec_id or f"{side}{day}{qty}",
        account_ref="ibkr:0000",
        symbol="SLV",
        contract="SLV",
        side=side,  # type: ignore[arg-type]
        quantity=Decimal(qty),
        price=Decimal(price),
        multiplier=Decimal(1),
        fees=Decimal(fee),
        executed_at=datetime(2026, 9, day, 15, tzinfo=UTC),
    )


def test_positions_fifo_realized_pnl_and_holding_time() -> None:
    fills = [fill("buy", "10", "30", 1), fill("buy", "10", "32", 2), fill("sell", "20", "35", 5)]
    (position,) = build_positions(fills)
    assert position.state == "closed"
    # (35-30)x10 + (35-32)x10 - 3 fees = 77.
    assert position.realized_pnl == Decimal(77)
    assert position.avg_entry == Decimal(31) and position.avg_exit == Decimal(35)
    assert position.max_quantity == Decimal(20)
    assert position.holding_days == 4


def test_partial_close_stays_open_and_a_new_round_trip_starts_after_flat() -> None:
    partial = build_positions([fill("buy", "10", "30", 1), fill("sell", "4", "33", 3)])
    assert partial[0].state == "open" and partial[0].quantity == Decimal(6)
    assert partial[0].realized_pnl == Decimal(10)  # (33-30)x4 - 2 fees
    two = build_positions(
        [fill("buy", "5", "30", 1), fill("sell", "5", "31", 2), fill("buy", "5", "29", 3)]
    )
    assert [p.state for p in two] == ["closed", "open"]


def test_plan_adherence() -> None:
    lows = {date(2026, 9, 2): Decimal(29), date(2026, 9, 3): Decimal("27.5")}
    ok = adherence(
        max_quantity=Decimal(10),
        planned_size=10,
        stop=Decimal(28),
        direction="long",
        daily_lows=lows,
        daily_highs={},
        closed_on=date(2026, 9, 3),
        invalidated_on=None,
    )
    assert ok == {"size_within_plan": True, "stop_honored": True, "exit_on_invalidation": None}
    late = adherence(
        max_quantity=Decimal(15),
        planned_size=10,
        stop=Decimal(28),
        direction="long",
        daily_lows=lows,
        daily_highs={},
        closed_on=date(2026, 9, 8),
        invalidated_on=date(2026, 9, 4),
    )
    assert late == {"size_within_plan": False, "stop_honored": False, "exit_on_invalidation": False}


def test_entry_anchor_is_the_last_close_known_at_the_time() -> None:
    from zoneinfo import ZoneInfo

    from desk.scoring.run import Bars

    ny = ZoneInfo("America/New_York")
    history = [DailyBar(date(2026, 9, d), 1, 1, 1, float(d), None) for d in (22, 23, 24)]
    stored = Bars.__new__(Bars)
    stored._tz = ny
    stored._cache = {"X": history}
    morning = stored.entry("X", datetime(2026, 9, 24, 7, tzinfo=ny))
    evening = stored.entry("X", datetime(2026, 9, 24, 17, tzinfo=ny))
    assert morning is not None and morning[0].day == date(2026, 9, 23)
    assert [b.day for b in morning[1]] == [date(2026, 9, 24)]
    assert evening is not None and evening[0].day == date(2026, 9, 24) and evening[1] == []
