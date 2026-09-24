"""Watch desk rules: pure functions from computed features to hits, plus importance.

No I/O here. Features are computed from stored bars and quotes by desk.watch.features;
every number a hit reports is carried as an ObservedValue with the data it came from.
"""

import math
from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from desk.artifacts.trigger import ObservedValue
from desk.config import CONFIG_DIR

UNSIZED_STRENGTH = 0.75
PLACES = Decimal("0.0001")


class PolicyGroup(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    keywords: tuple[str, ...]
    instruments: tuple[str, ...] = ()


class EventAdditions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    trading_days: int = Field(gt=0)
    rules: tuple[str, ...]
    max_per_day: int = Field(default=10, gt=0)


class ScanCadence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    intraday_tier01_regular_seconds: int = Field(gt=0)
    intraday_tier01_extended_seconds: int = Field(gt=0)
    intraday_tier2_regular_seconds: int = Field(gt=0)
    futures_overnight_seconds: int = Field(gt=0)
    close_after_minutes: int = Field(ge=0)
    news_seconds: int = Field(gt=0)
    commodity_seconds: int = Field(gt=0)


class WatchSchedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    shifts: dict[str, time]
    sunday_futures: time
    post_market_after_early_close_minutes: int = Field(ge=0)
    shift_grace_minutes: int = Field(gt=0)
    # Pre-market ratings and debates start no new work this long before the briefing.
    analyst_stop_before_briefing_minutes: int = Field(gt=0)
    scans: ScanCadence


class WatchConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    urgent_threshold: float = Field(ge=0, le=1)
    tier_weights: dict[int, float]
    default_cooldown_minutes: int = Field(gt=0)
    rules: dict[str, dict[str, Any]]
    schedule: WatchSchedule
    event_additions: EventAdditions

    def rule(self, rule_id: str) -> dict[str, Any]:
        return self.rules[rule_id]

    def policy_groups(self) -> dict[str, PolicyGroup]:
        groups = self.rules["policy_keywords"]["groups"]
        return {name: PolicyGroup.model_validate(value) for name, value in groups.items()}


def load_watch_config(config_dir: Path = CONFIG_DIR) -> WatchConfig:
    with (config_dir / "watch.yaml").open(encoding="utf-8") as handle:
        return WatchConfig.model_validate(yaml.safe_load(handle))


def dec(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(PLACES)


@dataclass(frozen=True, slots=True)
class Hit:
    rule_id: str
    instrument: str
    summary: str
    fingerprint: str
    # How far past the rule's threshold, in the rule's units; None for unsized rules.
    excess: float | None
    observed: tuple[ObservedValue, ...] = ()
    parents: tuple[Any, ...] = ()
    cooldown_minutes: int | None = None


def strength(excess: float | None, scale: float) -> float:
    if excess is None:
        return UNSIZED_STRENGTH
    return 0.5 + 0.5 * (1.0 - math.exp(-max(excess, 0.0) / scale))


def importance(hit: Hit, tier: int | None, config: WatchConfig) -> float:
    rule = config.rule(hit.rule_id)
    tier_weight = config.tier_weights.get(tier if tier is not None else 3, 0.35)
    value = (
        float(rule["weight"]) * tier_weight * strength(hit.excess, float(rule.get("scale", 1.0)))
    )
    return round(min(max(value, 0.0), 1.0), 4)


@dataclass(frozen=True, slots=True)
class DailyBar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float | None


@dataclass(slots=True)
class Features:
    """Everything the price rules read for one symbol. Refs name the underlying rows."""

    symbol: str
    bars: list[DailyBar]  # completed daily bars, oldest first, today excluded
    daily_ref: str  # e.g. "price_bars:yahoo:NVDA:1d"
    last: float | None = None
    last_ref: str | None = None
    day_open: float | None = None
    day_volume: float | None = None
    minutes_since_open: float | None = None
    session_minutes: float = 390.0
    iv_rank: float | None = None
    iv_ref: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def prev_close(self) -> float | None:
        return self.bars[-1].close if self.bars else None

    def atr(self, days: int = 20) -> float | None:
        if len(self.bars) < days + 1:
            return None
        window = self.bars[-(days + 1) :]
        ranges = [max(b.high, prev.close) - min(b.low, prev.close) for prev, b in pairwise(window)]
        return sum(ranges) / len(ranges)

    def prior_high_low(self, days: int) -> tuple[float, float] | None:
        if len(self.bars) < days:
            return None
        window = self.bars[-days:]
        return max(b.high for b in window), min(b.low for b in window)

    def avg_volume(self, days: int = 20) -> float | None:
        volumes = [b.volume for b in self.bars[-days:] if b.volume]
        return sum(volumes) / len(volumes) if len(volumes) >= days // 2 else None


def _intraday_ref(f: Features, today: date) -> str:
    return f"price_bars:tastytrade:{f.symbol}:1m:{today.isoformat()}"


def _range_ref(f: Features, days: int) -> str:
    window = f.bars[-days:]
    return f"{f.daily_ref}:{window[0].day.isoformat()}..{window[-1].day.isoformat()}"


def rule_move_vs_prior_close(
    f: Features, tier: int, config: WatchConfig, today: date
) -> Hit | None:
    rule = config.rule("move_vs_prior_close")
    threshold = float(rule["atr_multiple"].get(tier, rule["atr_multiple"][2]))
    atr, prev = f.atr(), f.prev_close
    if f.last is None or atr is None or prev is None or atr <= 0:
        return None
    multiple = (f.last - prev) / atr
    if abs(multiple) < threshold:
        return None
    direction = "up" if multiple > 0 else "down"
    return Hit(
        rule_id="move_vs_prior_close",
        instrument=f.symbol,
        summary=f"{f.symbol} {direction} {abs(multiple):.1f} ATR from prior close",
        fingerprint=f"move_vs_prior_close:{f.symbol}:{today.isoformat()}:{direction}",
        excess=abs(multiple) - threshold,
        observed=(
            ObservedValue(name="last", value=dec(f.last), unit="USD", source_ref=f.last_ref or ""),
            ObservedValue(
                name="prior_close",
                value=dec(prev),
                unit="USD",
                source_ref=f"{f.daily_ref}:{f.bars[-1].day.isoformat()}",
            ),
            ObservedValue(
                name="atr20",
                value=dec(atr),
                unit="USD",
                source_ref=f"computed:atr20({_range_ref(f, 21)})",
            ),
            ObservedValue(
                name="atr_multiple",
                value=dec(multiple),
                unit="ATR",
                source_ref="computed:(last-prior_close)/atr20",
            ),
        ),
    )


def rule_gap_open(f: Features, tier: int, config: WatchConfig, today: date) -> Hit | None:
    rule = config.rule("gap_open")
    atr, prev = f.atr(), f.prev_close
    if (
        f.day_open is None
        or atr is None
        or prev is None
        or atr <= 0
        or f.minutes_since_open is None
        or f.minutes_since_open > float(rule["window_minutes"])
    ):
        return None
    multiple = (f.day_open - prev) / atr
    threshold = float(rule["atr_multiple"])
    if abs(multiple) < threshold:
        return None
    direction = "up" if multiple > 0 else "down"
    return Hit(
        rule_id="gap_open",
        instrument=f.symbol,
        summary=f"{f.symbol} gapped {direction} {abs(multiple):.1f} ATR at the open",
        fingerprint=f"gap_open:{f.symbol}:{today.isoformat()}",
        excess=abs(multiple) - threshold,
        observed=(
            ObservedValue(
                name="day_open",
                value=dec(f.day_open),
                unit="USD",
                source_ref=str(f.extra.get("open_ref") or _intraday_ref(f, today)),
            ),
            ObservedValue(
                name="prior_close",
                value=dec(prev),
                unit="USD",
                source_ref=f"{f.daily_ref}:{f.bars[-1].day.isoformat()}",
            ),
            ObservedValue(
                name="gap_atr_multiple",
                value=dec(multiple),
                unit="ATR",
                source_ref="computed:(day_open-prior_close)/atr20",
            ),
        ),
    )


def rule_range_break(f: Features, tier: int, config: WatchConfig, today: date) -> Hit | None:
    rule = config.rule("range_break")
    days = int(rule["lookback_days"])
    bounds, atr = f.prior_high_low(days), f.atr()
    if f.last is None or bounds is None or atr is None or atr <= 0:
        return None
    high, low = bounds
    if f.last > high:
        direction, level, excess = "above", high, (f.last - high) / atr
    elif f.last < low:
        direction, level, excess = "below", low, (low - f.last) / atr
    else:
        return None
    return Hit(
        rule_id="range_break",
        instrument=f.symbol,
        summary=f"{f.symbol} broke {direction} its {days}-day range",
        fingerprint=f"range_break:{f.symbol}:{today.isoformat()}:{direction}",
        excess=excess,
        observed=(
            ObservedValue(name="last", value=dec(f.last), unit="USD", source_ref=f.last_ref or ""),
            ObservedValue(
                name=f"{days}d_{'high' if direction == 'above' else 'low'}",
                value=dec(level),
                unit="USD",
                source_ref=_range_ref(f, days),
            ),
        ),
    )


def rule_volume_pace(f: Features, tier: int, config: WatchConfig, today: date) -> Hit | None:
    rule = config.rule("volume_pace")
    avg = f.avg_volume()
    if (
        f.day_volume is None
        or avg is None
        or avg <= 0
        or f.minutes_since_open is None
        or f.minutes_since_open < float(rule["min_minutes"])
    ):
        return None
    elapsed = min(f.minutes_since_open / f.session_minutes, 1.0)
    pace = f.day_volume / (avg * elapsed)
    threshold = float(rule["pace_multiple"])
    if pace < threshold:
        return None
    return Hit(
        rule_id="volume_pace",
        instrument=f.symbol,
        summary=f"{f.symbol} volume running {pace:.1f}x its 20-day pace",
        fingerprint=f"volume_pace:{f.symbol}:{today.isoformat()}",
        excess=pace - threshold,
        observed=(
            ObservedValue(
                name="day_volume",
                value=dec(f.day_volume),
                unit="shares",
                source_ref=str(f.extra.get("volume_ref") or _intraday_ref(f, today)),
            ),
            ObservedValue(
                name="avg_volume_20d",
                value=dec(avg),
                unit="shares",
                source_ref=f"computed:avg_volume20({_range_ref(f, 20)})",
            ),
            ObservedValue(
                name="pace_multiple",
                value=dec(pace),
                unit="x",
                source_ref="computed:day_volume/(avg_volume_20d*elapsed_fraction)",
            ),
        ),
    )


def rule_new_52w(f: Features, tier: int, config: WatchConfig, today: date) -> Hit | None:
    if len(f.bars) < 250:
        return None
    latest, prior = f.bars[-1], f.bars[-252:-1]
    if latest.close > max(b.high for b in prior):
        direction, level = "high", max(b.high for b in prior)
    elif latest.close < min(b.low for b in prior):
        direction, level = "low", min(b.low for b in prior)
    else:
        return None
    return Hit(
        rule_id="new_52w",
        instrument=f.symbol,
        summary=f"{f.symbol} closed at a 52-week {direction}",
        fingerprint=f"new_52w:{f.symbol}:{latest.day.isoformat()}:{direction}",
        excess=None,
        observed=(
            ObservedValue(
                name="close",
                value=dec(latest.close),
                unit="USD",
                source_ref=f"{f.daily_ref}:{latest.day.isoformat()}",
            ),
            ObservedValue(
                name=f"prior_52w_{direction}",
                value=dec(level),
                unit="USD",
                source_ref=_range_ref(f, 251),
            ),
        ),
    )


def rule_iv_rank_extreme(f: Features, tier: int, config: WatchConfig, today: date) -> Hit | None:
    rule = config.rule("iv_rank_extreme")
    if f.iv_rank is None:
        return None
    if f.iv_rank >= float(rule["high"]):
        label = "high"
    elif f.iv_rank <= float(rule["low"]):
        label = "low"
    else:
        return None
    return Hit(
        rule_id="iv_rank_extreme",
        instrument=f.symbol,
        summary=f"{f.symbol} IV rank at an extreme {label} ({f.iv_rank:.2f})",
        fingerprint=f"iv_rank_extreme:{f.symbol}:{today.isoformat()}",
        excess=None,
        observed=(
            ObservedValue(
                name="iv_rank", value=dec(f.iv_rank), unit="ratio", source_ref=f.iv_ref or ""
            ),
        ),
    )


INTRADAY_RULES = (rule_move_vs_prior_close, rule_gap_open, rule_range_break, rule_volume_pace)
CLOSE_RULES = (rule_new_52w, rule_iv_rank_extreme)


def relative_strength_hits(features: list[Features], config: WatchConfig, today: date) -> list[Hit]:
    """Top and bottom slice of the universe by N-day return."""
    rule = config.rule("relative_strength")
    days = int(rule["lookback_days"])
    returns = []
    for f in features:
        if len(f.bars) > days and f.bars[-days - 1].close > 0:
            returns.append((f.bars[-1].close / f.bars[-days - 1].close - 1.0, f))
    if len(returns) < 20:
        return []
    returns.sort(key=lambda item: item[0])
    count = max(1, int(len(returns) * float(rule["top_fraction"])))
    hits = []
    for side, bucket in (("leader", returns[-count:]), ("laggard", returns[:count])):
        for value, f in bucket:
            hits.append(
                Hit(
                    rule_id="relative_strength",
                    instrument=f.symbol,
                    summary=f"{f.symbol} is a {days}-day relative-strength {side} ({value:+.1%})",
                    fingerprint=f"relative_strength:{f.symbol}:{f.bars[-1].day.isoformat()}:{side}",
                    excess=None,
                    observed=(
                        ObservedValue(
                            name=f"return_{days}d",
                            value=dec(value),
                            unit="ratio",
                            source_ref=f"computed:return({_range_ref(f, days + 1)})",
                        ),
                    ),
                )
            )
    return hits


def percentile_rank(values: list[float], latest: float) -> float:
    """Share of `values` at or below `latest`."""
    return sum(1 for v in values if v <= latest) / len(values)


def zscore(sample: list[float], value: float) -> float | None:
    if len(sample) < 3:
        return None
    mean = sum(sample) / len(sample)
    variance = sum((x - mean) ** 2 for x in sample) / (len(sample) - 1)
    if variance <= 0:
        return None
    return (value - mean) / math.sqrt(variance)
