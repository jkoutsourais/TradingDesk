"""Typed loaders for the YAML files in config/."""

from datetime import UTC, date, datetime, time, timedelta
from functools import cached_property
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from desk.settings import REPO_ROOT

CONFIG_DIR = REPO_ROOT / "config"

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAY_INDEX: dict[str, int] = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Window(_Frozen):
    """Daily local-time window on selected weekdays; `end` is exclusive and after `start`."""

    days: tuple[Weekday, ...]
    start: time
    end: time

    @field_validator("end")
    @classmethod
    def _end_after_start(cls, end: time, info: ValidationInfo) -> time:
        start = info.data.get("start")
        if start is not None and end <= start:
            raise ValueError("window end must be after start (overnight windows unsupported)")
        return end

    def _day_span(self, day: date, tz: ZoneInfo) -> tuple[datetime, datetime] | None:
        if day.weekday() not in {WEEKDAY_INDEX[d] for d in self.days}:
            return None
        return (
            datetime.combine(day, self.start, tzinfo=tz).astimezone(UTC),
            datetime.combine(day, self.end, tzinfo=tz).astimezone(UTC),
        )

    def contains(self, moment: datetime, tz: ZoneInfo) -> bool:
        span = self._day_span(moment.astimezone(tz).date(), tz)
        return span is not None and span[0] <= moment < span[1]

    def active_seconds(self, start: datetime, end: datetime, tz: ZoneInfo) -> float:
        """Seconds of [start, end) that fall inside the window."""
        total = 0.0
        day = start.astimezone(tz).date()
        last_day = end.astimezone(tz).date()
        while day <= last_day:
            span = self._day_span(day, tz)
            if span is not None:
                overlap = min(end, span[1]) - max(start, span[0])
                total += max(0.0, overlap.total_seconds())
            day += timedelta(days=1)
        return total

    def next_open(self, moment: datetime, tz: ZoneInfo) -> datetime:
        """Start of the next window span at or after `moment`."""
        day = moment.astimezone(tz).date()
        for offset in range(8):
            span = self._day_span(day + timedelta(days=offset), tz)
            if span is not None and span[1] > moment:
                return max(span[0], moment)
        raise ValueError("window has no active days")


class CollectorSchedule(_Frozen):
    interval_seconds: int = Field(gt=0)
    window: Window | None = None


class Retention(_Frozen):
    raw_record_days: int = Field(gt=0)


class ScheduleConfig(_Frozen):
    timezone: str
    collectors: dict[str, CollectorSchedule]
    retention: Retention
    run_timeout_seconds: int = Field(gt=0)

    @cached_property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class TierGroup(_Frozen):
    stocks: tuple[str, ...] = ()
    etfs: tuple[str, ...] = ()
    futures: tuple[str, ...] = ()


class Tier3(_Frozen):
    symbols: tuple[str, ...]


class TiersConfig(_Frozen):
    tier_1: dict[str, TierGroup]
    tier_3_macro: Tier3

    def tier_1_stocks(self) -> tuple[str, ...]:
        return tuple(sorted({s for group in self.tier_1.values() for s in group.stocks}))

    def tier_1_symbols(self) -> tuple[str, ...]:
        symbols = {
            symbol
            for group in self.tier_1.values()
            for symbol in (*group.stocks, *group.etfs, *group.futures)
        }
        return tuple(sorted(symbols))


class FredSources(_Frozen):
    lookback_days: int = Field(gt=0)
    series: dict[str, str]


class EiaSources(_Frozen):
    lookback_weeks: int = Field(gt=0)
    series: dict[str, str]


class CotSources(_Frozen):
    dataset: str
    lookback_weeks: int = Field(gt=0)
    contracts: dict[str, str]
    fields: tuple[str, ...]


class GridPrices(_Frozen):
    method: Literal["get_spp", "get_lmp"]
    market: str
    location_type: str | None = None
    locations: tuple[str, ...]


class GridIso(_Frozen):
    requires_key: bool = False
    load: bool = False
    fuel_mix: bool = False
    prices: GridPrices | None = None


class GridSources(_Frozen):
    settle_minutes: int = Field(gt=0)
    isos: dict[str, GridIso]


class SourcesConfig(_Frozen):
    fred: FredSources
    eia: EiaSources
    grid: GridSources
    cftc_cot: CotSources


def _load_yaml(path: Path) -> object:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_schedule(config_dir: Path = CONFIG_DIR) -> ScheduleConfig:
    return ScheduleConfig.model_validate(_load_yaml(config_dir / "schedule.yaml"))


def load_tiers(config_dir: Path = CONFIG_DIR) -> TiersConfig:
    return TiersConfig.model_validate(_load_yaml(config_dir / "tiers.yaml"))


class EmbeddingModel(_Frozen):
    model: str
    dimensions: int = Field(gt=0)
    document_prefix: str = ""
    query_prefix: str = ""
    cpu_only: bool = True
    keep_alive: str = "10m"
    batch_size: int = Field(gt=0)


class ModelsConfig(_Frozen):
    embedding: EmbeddingModel


def load_models(config_dir: Path = CONFIG_DIR) -> ModelsConfig:
    return ModelsConfig.model_validate(_load_yaml(config_dir / "models.yaml"))


class UniverseConfig(_Frozen):
    source: str
    as_of: date
    symbols: tuple[str, ...]


def load_universe(config_dir: Path = CONFIG_DIR) -> UniverseConfig:
    return UniverseConfig.model_validate(_load_yaml(config_dir / "universe_sp500.yaml"))


def load_sources(config_dir: Path = CONFIG_DIR) -> SourcesConfig:
    return SourcesConfig.model_validate(_load_yaml(config_dir / "sources.yaml"))
