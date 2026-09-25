"""Price levels computed by code, offered to the thesis writer as invalidation choices.

The writer picks level ids; it never writes a level itself. Each level names the daily
bars it came from, so a thesis's invalidation traces back to stored prices.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise

from desk.watch.rules import DailyBar

ATR_DAYS = 20
RANGE_DAYS = 20
YEAR_DAYS = 252


@dataclass(frozen=True, slots=True)
class Level:
    id: str  # "lvl_<n>", used as a placeholder by the thesis writer
    instrument: str
    name: str  # e.g. "low_20d"
    label: str  # e.g. "SLV 20-day low"
    value: Decimal
    ref: str  # e.g. "levels:SLV:low_20d:2026-09-22"


def _round(value: float) -> Decimal:
    step = Decimal("0.01") if value >= 1 else Decimal("0.0001")
    return Decimal(str(value)).quantize(step, rounding=ROUND_HALF_UP)


def atr(bars: list[DailyBar], days: int = ATR_DAYS) -> float | None:
    if len(bars) < days + 1:
        return None
    window = bars[-(days + 1) :]
    ranges = [max(b.high, prev.close) - min(b.low, prev.close) for prev, b in pairwise(window)]
    return sum(ranges) / len(ranges)


def level_menu(instrument: str, bars: list[DailyBar], start_index: int = 1) -> list[Level]:
    """Levels below and above the last close: ranges, 52-week extremes and ATR steps.

    `bars` are completed daily bars, oldest first. Returns nothing without a close.
    """
    if not bars:
        return []
    last = bars[-1]
    as_of = last.day.isoformat()
    candidates: list[tuple[str, str, float]] = [("last_close", "last close", last.close)]
    if len(bars) >= RANGE_DAYS:
        window = bars[-RANGE_DAYS:]
        candidates += [
            ("low_20d", "20-day low", min(b.low for b in window)),
            ("high_20d", "20-day high", max(b.high for b in window)),
        ]
    if len(bars) >= YEAR_DAYS // 2:
        year = bars[-YEAR_DAYS:]
        candidates += [
            ("low_52w", "52-week low", min(b.low for b in year)),
            ("high_52w", "52-week high", max(b.high for b in year)),
        ]
    step = atr(bars)
    if step is not None:
        for multiple in (1, 2, 3):
            candidates += [
                (
                    f"close_minus_{multiple}atr",
                    f"{multiple} ATR below the last close",
                    last.close - multiple * step,
                ),
                (
                    f"close_plus_{multiple}atr",
                    f"{multiple} ATR above the last close",
                    last.close + multiple * step,
                ),
            ]
    levels = []
    for offset, (name, label, value) in enumerate(
        (c for c in candidates if c[2] > 0), start=start_index
    ):
        levels.append(
            Level(
                id=f"lvl_{offset}",
                instrument=instrument,
                name=name,
                label=f"{instrument} {label}",
                value=_round(value),
                ref=f"levels:{instrument}:{name}:{as_of}",
            )
        )
    return levels
