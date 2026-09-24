"""Idea lanes: watch triggers and code screens become scored LaneCandidates.

Every lane emits signals (an instrument, an importance, a driver line and the row it came
from). Signals for the same lane and instrument combine into one candidate:

    score = min(1, lane weight x (1 - prod(1 - importance)) + claim bonus)

where the claim bonus counts verified claims on the instrument from recent research.
No model is involved.
"""

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from desk.artifacts.idea import LaneCandidate, ScoreIngredient
from desk.artifacts.thesis import Lane
from desk.config import CONFIG_DIR, TiersConfig
from desk.desks.research import combine_importance
from desk.watch.rules import PolicyGroup, strength
from desk.watch.scan import TierMap


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class LaneSettings(_Frozen):
    weight: float = Field(gt=0, le=1)
    rules: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()


class Selection(_Frozen):
    shortlist: int = Field(gt=0)
    theses: int = Field(gt=0)


class FredScreen(_Frozen):
    label: str
    unit: str  # "bp" (series in percent, change shown in basis points) or "pct"
    change_days: int = Field(gt=0)
    threshold: float = Field(gt=0)
    scale: float = Field(gt=0)
    when_down: tuple[str, ...]
    when_up: tuple[str, ...]


class MacroSettings(_Frozen):
    symbol_proxies: dict[str, tuple[str, ...]]
    fred: dict[str, FredScreen]


class GridSettings(_Frozen):
    min_days: int = Field(gt=1)
    ratio: float = Field(gt=1)
    scale: float = Field(gt=0)
    instruments: dict[str, tuple[str, ...]]


class LanesConfig(_Frozen):
    trigger_window_hours: int = Field(gt=0)
    claim_window_days: int = Field(gt=0)
    claim_bonus_per_claim: float = Field(ge=0)
    claim_bonus_cap: float = Field(ge=0)
    selection: Selection
    lanes: dict[Lane, LaneSettings]
    macro: MacroSettings
    grid: GridSettings


def load_lanes_config(config_dir: Path = CONFIG_DIR) -> LanesConfig:
    with (config_dir / "lanes.yaml").open(encoding="utf-8") as handle:
        return LanesConfig.model_validate(yaml.safe_load(handle))


@dataclass(frozen=True, slots=True)
class TriggerRow:
    id: UUID
    rule_id: str
    instrument: str
    importance: float
    summary: str


@dataclass(frozen=True, slots=True)
class Signal:
    lane: Lane
    instrument: str
    importance: float
    driver: str
    source_ref: str
    parent: UUID | None = None


# --- Routing ------------------------------------------------------------------------------


def group_of(tiers: TiersConfig) -> dict[str, str]:
    return {
        symbol: name
        for name, group in tiers.tier_1.items()
        for symbol in (*group.stocks, *group.etfs, *group.futures)
    }


def route(
    trigger: TriggerRow, tier: int | None, groups: dict[str, str], config: LanesConfig
) -> Lane | None:
    """The lane a trigger feeds; policy and macro topics are expanded by the caller."""
    lanes = config.lanes
    rule = trigger.rule_id
    if rule == "policy_keywords":
        return "policy"
    if rule in lanes["commodity"].rules:
        return "commodity"
    if trigger.instrument in config.macro.symbol_proxies:
        return "macro"
    group = groups.get(trigger.instrument)
    if trigger.instrument.startswith("/") or group in lanes["commodity"].groups:
        return "commodity"
    if group in lanes["power_grid"].groups:
        return "power_grid"
    if tier not in (0, 1) and rule in lanes["event_additions"].rules:
        return "event_additions"
    if rule in lanes["catalyst"].rules:
        return "catalyst"
    if rule in lanes["screen"].rules:
        return "screen"
    return None


def trigger_signals(
    triggers: list[TriggerRow],
    tier_map: TierMap,
    groups: dict[str, str],
    policy_groups: dict[str, PolicyGroup],
    config: LanesConfig,
) -> list[Signal]:
    signals = []
    for trigger in triggers:
        lane = route(trigger, tier_map.tier(trigger.instrument), groups, config)
        if lane is None:
            continue
        instruments: tuple[str, ...] = (trigger.instrument,)
        if lane == "policy":
            topic = trigger.instrument.removeprefix("policy:")
            instruments = tuple(policy_groups[topic].instruments) if topic in policy_groups else ()
        elif lane == "macro" and trigger.instrument in config.macro.symbol_proxies:
            instruments = config.macro.symbol_proxies[trigger.instrument]
        for instrument in instruments:
            signals.append(
                Signal(
                    lane,
                    instrument,
                    trigger.importance,
                    trigger.summary,
                    f"trigger:{trigger.id}",
                    trigger.id,
                )
            )
    return signals


# --- Screens ------------------------------------------------------------------------------


def fred_signals(series: dict[str, list[tuple[date, float]]], config: LanesConfig) -> list[Signal]:
    """20-day moves in real yields, nominal yields and the dollar, mapped to proxies."""
    signals = []
    for series_id, screen in config.macro.fred.items():
        values = sorted(series.get(series_id, []))
        if len(values) <= screen.change_days:
            continue
        (start_day, start), (end_day, end) = values[-(screen.change_days + 1)], values[-1]
        if screen.unit == "bp":
            change = (end - start) * 100
            shown = f"{change:+.0f} bp"
        else:
            change = (end / start - 1) * 100
            shown = f"{change:+.1f}%"
        if abs(change) < screen.threshold:
            continue
        importance = strength(abs(change) - screen.threshold, screen.scale)
        direction = "down" if change < 0 else "up"
        driver = f"{screen.label} {direction} {shown} over {screen.change_days} trading days"
        ref = f"series_observations:fred:{series_id}:{start_day}..{end_day}"
        for instrument in screen.when_down if change < 0 else screen.when_up:
            signals.append(Signal("macro", instrument, importance, driver, ref))
    return signals


def grid_signals(peaks: dict[str, list[tuple[date, float]]], config: LanesConfig) -> list[Signal]:
    """A hub's latest daily peak price against the median peak of the prior days."""
    signals = []
    grid = config.grid
    for key, days in peaks.items():
        iso = key.split(":", 1)[0]
        ordered = sorted(days)
        if len(ordered) < grid.min_days or iso not in grid.instruments:
            continue
        latest_day, latest = ordered[-1]
        baseline = statistics.median(value for _, value in ordered[:-1])
        if baseline <= 0 or latest / baseline < grid.ratio:
            continue
        ratio = latest / baseline
        importance = strength(ratio - grid.ratio, grid.scale)
        driver = f"{key} peak price {ratio:.1f}x its recent median peak"
        ref = f"grid_observations:{key}:{latest_day}"
        for instrument in grid.instruments[iso]:
            signals.append(Signal("power_grid", instrument, importance, driver, ref))
    return signals


# --- Candidates ---------------------------------------------------------------------------


def combine(
    signals: list[Signal],
    claim_counts: dict[str, int],
    config: LanesConfig,
    shift_id: UUID | None = None,
) -> list[LaneCandidate]:
    grouped: dict[tuple[Lane, str], list[Signal]] = defaultdict(list)
    for signal in signals:
        grouped[(signal.lane, signal.instrument)].append(signal)
    candidates = []
    for (lane, instrument), members in grouped.items():
        members.sort(key=lambda s: -s.importance)
        weight = config.lanes[lane].weight
        base = weight * combine_importance([s.importance for s in members])
        claims = claim_counts.get(instrument, 0)
        bonus = min(claims * config.claim_bonus_per_claim, config.claim_bonus_cap)
        ingredients = [
            ScoreIngredient(
                name="importance", value=round(s.importance, 4), source_ref=s.source_ref
            )
            for s in members
        ]
        ingredients.append(
            ScoreIngredient(name="lane weight", value=weight, source_ref="config:lanes.yaml")
        )
        if bonus:
            ingredients.append(
                ScoreIngredient(
                    name=f"verified claims ({claims})",
                    value=round(bonus, 4),
                    source_ref=f"verified_claims:{instrument}",
                )
            )
        parents = tuple(dict.fromkeys(s.parent for s in members if s.parent is not None))
        candidates.append(
            LaneCandidate(
                produced_by=f"idea.lanes.{lane}",
                runtime_ms=0,
                shift_id=shift_id,
                parents=parents,
                lane=lane,
                instrument=instrument,
                driver=members[0].driver[:300],
                score=round(min(base + bonus, 1.0), 4),
                ingredients=tuple(ingredients),
            )
        )
    candidates.sort(key=lambda c: (-c.score, c.lane, c.instrument))
    return candidates


# --- Database -----------------------------------------------------------------------------


def load_triggers(conn: Connection, since: datetime) -> list[TriggerRow]:
    rows = conn.execute(
        text(
            "SELECT id, payload->>'rule_id' AS rule_id, payload->>'instrument' AS instrument, "
            "(payload->>'importance')::float AS importance, payload->>'summary' AS summary "
            "FROM artifacts WHERE kind = 'trigger' AND status = 'ok' AND created_at >= :since"
        ),
        {"since": since},
    ).all()
    return [TriggerRow(r.id, r.rule_id, r.instrument, r.importance, r.summary) for r in rows]


def load_claim_counts(conn: Connection, since: datetime) -> dict[str, int]:
    rows = conn.execute(
        text(
            "SELECT c.payload->>'subject' AS subject, count(DISTINCT c.id) AS n "
            "FROM artifacts v JOIN artifacts c ON c.id = (v.payload->>'claim_id')::uuid "
            "WHERE v.kind = 'verified_claim' AND v.status = 'ok' "
            "AND v.payload->>'verdict' IN ('verified', 'corrected') AND v.created_at >= :since "
            "GROUP BY 1"
        ),
        {"since": since},
    ).all()
    return {row.subject: int(row.n) for row in rows}


def load_fred(conn: Connection, series_ids: list[str]) -> dict[str, list[tuple[date, float]]]:
    rows = conn.execute(
        text(
            "SELECT DISTINCT ON (series_id, period) series_id, period, value "
            "FROM series_observations WHERE source = 'fred' AND series_id = ANY(:ids) "
            "AND value IS NOT NULL ORDER BY series_id, period, fetched_at DESC"
        ),
        {"ids": series_ids},
    ).all()
    series: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for row in rows:
        series[row.series_id].append((row.period, float(row.value)))
    return series


def load_grid_peaks(
    conn: Connection, since: datetime, tz: ZoneInfo
) -> dict[str, list[tuple[date, float]]]:
    rows = conn.execute(
        text(
            "SELECT iso, series_id, (interval_start AT TIME ZONE :tz)::date AS day, "
            "max(value) AS peak FROM grid_observations "
            "WHERE series_id LIKE 'price.%' AND interval_start >= :since GROUP BY 1, 2, 3"
        ),
        {"since": since, "tz": str(tz)},
    ).all()
    peaks: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for row in rows:
        peaks[f"{row.iso}:{row.series_id}"].append((row.day, float(row.peak)))
    return peaks


def build_candidates(
    conn: Connection,
    config: LanesConfig,
    tier_map: TierMap,
    tiers: TiersConfig,
    policy_groups: dict[str, PolicyGroup],
    now: datetime,
    tz: ZoneInfo,
    shift_id: UUID | None = None,
) -> list[LaneCandidate]:
    signals = trigger_signals(
        load_triggers(conn, now - timedelta(hours=config.trigger_window_hours)),
        tier_map,
        group_of(tiers),
        policy_groups,
        config,
    )
    signals += fred_signals(load_fred(conn, list(config.macro.fred)), config)
    signals += grid_signals(load_grid_peaks(conn, now - timedelta(days=30), tz), config)
    claims = load_claim_counts(conn, now - timedelta(days=config.claim_window_days))
    return combine(signals, claims, config, shift_id)
