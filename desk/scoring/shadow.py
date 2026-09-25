"""Shadow scoring: what an idea or plan would have done, from stored daily bars alone.

Returns are direction-adjusted percent moves from the entry price to the close at the
horizon. Excursions use the bars' highs and lows. When a stop and target are known, the
first one touched decides the R-multiple; a bar that touches both counts as the stop,
since daily bars cannot tell which came first.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from desk.watch.rules import DailyBar

Direction = Literal["long", "short"]
HORIZONS = {"1d": 1, "5d": 5, "20d": 20}


@dataclass(frozen=True, slots=True)
class PathScore:
    return_pct: float
    mae_pct: float  # worst direction-adjusted move, zero or negative
    mfe_pct: float  # best direction-adjusted move, zero or positive
    first_hit: Literal["stop", "target", "none"]
    r_multiple: float | None
    exit_day: date
    exit_price: float


def score_path(
    direction: Direction,
    entry: Decimal,
    stop: Decimal | None,
    target: Decimal | None,
    bars_after: list[DailyBar],
    horizon: int,
) -> PathScore | None:
    """Score `horizon` trading days after entry; None until that many bars exist."""
    if len(bars_after) < horizon or entry <= 0:
        return None
    window = bars_after[:horizon]
    price = float(entry)
    sign = 1.0 if direction == "long" else -1.0
    exit_close = window[-1].close
    lowest = min(b.low for b in window)
    highest = max(b.high for b in window)
    if direction == "long":
        adverse, favorable = lowest / price - 1, highest / price - 1
    else:
        adverse, favorable = 1 - highest / price, 1 - lowest / price

    first_hit: Literal["stop", "target", "none"] = "none"
    for bar in window:
        stop_hit = stop is not None and (
            bar.low <= float(stop) if direction == "long" else bar.high >= float(stop)
        )
        target_hit = target is not None and (
            bar.high >= float(target) if direction == "long" else bar.low <= float(target)
        )
        if stop_hit:
            first_hit = "stop"
            break
        if target_hit:
            first_hit = "target"
            break

    r_multiple = None
    if stop is not None and float(stop) != price:
        risk = abs(price - float(stop))
        if first_hit == "stop":
            r_multiple = -1.0
        elif first_hit == "target" and target is not None:
            r_multiple = sign * (float(target) - price) / risk
        else:
            r_multiple = sign * (exit_close - price) / risk
    return PathScore(
        return_pct=round(sign * (exit_close / price - 1) * 100, 4),
        mae_pct=round(min(adverse, 0.0) * 100, 4),
        mfe_pct=round(max(favorable, 0.0) * 100, 4),
        first_hit=first_hit,
        r_multiple=round(r_multiple, 4) if r_multiple is not None else None,
        exit_day=window[-1].day,
        exit_price=exit_close,
    )


def rating_hit(rating: str, long_return_pct: float) -> bool:
    """Buy/Add and Hold are right when the price rose; Trim and Sell when it fell."""
    return long_return_pct > 0 if rating in ("buy_add", "hold") else long_return_pct <= 0


def stance_hit(stance: str, thesis_return_pct: float) -> bool | None:
    if stance == "neutral":
        return None
    return thesis_return_pct > 0 if stance == "for" else thesis_return_pct <= 0


def verdict_hit(verdict: str, thesis_return_pct: float) -> bool | None:
    if verdict == "pursue":
        return thesis_return_pct > 0
    if verdict == "reject":
        return thesis_return_pct <= 0
    return None


def _within_one_session(event: date, closed_on: date | None, sessions: list[date]) -> bool:
    if closed_on is None:
        return False
    following = next((day for day in sessions if day > event), event + timedelta(days=1))
    return closed_on <= following


def adherence(
    max_quantity: Decimal,
    planned_size: int | None,
    stop: Decimal | None,
    direction: Direction,
    daily_lows: dict[date, Decimal],
    daily_highs: dict[date, Decimal],
    closed_on: date | None,
    invalidated_on: date | None,
) -> dict[str, bool | None]:
    """Stop honored, size within plan, exit on invalidation (None where it does not apply).

    `daily_lows` and `daily_highs` cover the days the position was open. A stop or an
    invalidation is honored when the position was closed by the next session.
    """
    sessions = sorted(set(daily_lows) | set(daily_highs))
    stop_honored: bool | None = None
    if stop is not None:
        prices = daily_lows if direction == "long" else daily_highs
        breached = sorted(
            day
            for day, value in prices.items()
            if (value <= stop if direction == "long" else value >= stop)
        )
        stop_honored = (
            True if not breached else _within_one_session(breached[0], closed_on, sessions)
        )
    invalidation: bool | None = None
    if invalidated_on is not None:
        invalidation = _within_one_session(invalidated_on, closed_on, sessions)
    return {
        "size_within_plan": None if planned_size is None else abs(max_quantity) <= planned_size,
        "stop_honored": stop_honored,
        "exit_on_invalidation": invalidation,
    }
