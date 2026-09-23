"""Power grid data through the open-source gridstatus library: load, fuel mix, hub prices.

gridstatus is synchronous, so each operator runs in a worker thread. Only intervals that
ended at least `settle_minutes` ago are stored, which keeps preliminary values out of
the append-only table.
"""

import asyncio
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from desk.collectors.base import CollectResult, GridObservation
from desk.config import GridIso, GridPrices, GridSources

TIME_COLUMNS = {"Time", "Interval Start", "Interval End"}
DEFAULT_INTERVAL_MINUTES = 5
YESTERDAY_REREAD_HOURS = 2

IsoFactory = Callable[[str], Any]  # iso name -> gridstatus ISO object


def gridstatus_factory(pjm_api_key: str | None) -> IsoFactory:
    import logging

    import gridstatus

    logging.getLogger("gridstatus").setLevel(logging.WARNING)
    classes = {"ercot": gridstatus.Ercot, "miso": gridstatus.MISO, "caiso": gridstatus.CAISO}

    def make(iso: str) -> Any:
        if iso == "pjm":
            return gridstatus.PJM(api_key=pjm_api_key)
        return classes[iso]()

    return make


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _starts_and_minutes(frame: Any) -> tuple[list[datetime], int]:
    start_column = "Interval Start" if "Interval Start" in frame.columns else "Time"
    starts = [ts.to_pydatetime().astimezone(UTC) for ts in frame[start_column]]
    if "Interval End" in frame.columns and len(frame):
        minutes = int(
            (frame["Interval End"].iloc[0] - frame[start_column].iloc[0]).total_seconds() // 60
        )
    elif len(starts) > 1:
        gaps = sorted(int((b - a).total_seconds() // 60) for a, b in pairwise(starts))
        minutes = gaps[len(gaps) // 2]
    else:
        minutes = DEFAULT_INTERVAL_MINUTES
    return starts, max(minutes, 1)


def frame_to_observations(
    iso: str,
    frame: Any,
    columns: dict[str, str],
    unit: str,
    cutoff: datetime,
    fetched_at: datetime,
) -> list[GridObservation]:
    """`columns` maps frame column -> series_id; only intervals ending by `cutoff` are kept."""
    starts, minutes = _starts_and_minutes(frame)
    observations = []
    for row_index, start in enumerate(starts):
        if start + timedelta(minutes=minutes) > cutoff:
            continue
        for column, series_id in columns.items():
            value = frame[column].iloc[row_index]
            # pd.isna covers NaN, None and pandas' NA, whose comparisons raise instead.
            if pd.isna(value):
                continue
            observations.append(
                GridObservation(
                    iso, series_id, start, minutes, Decimal(f"{float(value):.4f}"), unit, fetched_at
                )
            )
    return observations


def fetch_dates(iso: Any, now: datetime) -> list[Any]:
    """Operators publish by local day; just after local midnight, yesterday is re-read so
    its final intervals are not lost between runs."""
    local_now = now.astimezone(ZoneInfo(iso.default_timezone))
    dates: list[Any] = ["today"]
    if local_now.hour < YESTERDAY_REREAD_HOURS:
        dates.append((local_now - timedelta(days=1)).date())
    return dates


def collect_iso(
    iso_name: str, config: GridIso, iso: Any, settle_minutes: int, now: datetime
) -> tuple[list[GridObservation], list[str]]:
    cutoff = now - timedelta(minutes=settle_minutes)
    fetched_at = datetime.now(UTC)
    observations: list[GridObservation] = []
    errors: list[str] = []
    dates = fetch_dates(iso, now)

    def attempt(label: str, fetch: Callable[[], list[GridObservation]]) -> None:
        try:
            observations.extend(fetch())
        except Exception as exc:  # noqa: BLE001 - one failing feed must not drop the others
            errors.append(f"{iso_name} {label}: {type(exc).__name__}: {str(exc)[:200]}")

    for day in dates:
        if config.load:

            def load(day: Any = day) -> list[GridObservation]:
                frame = iso.get_load(day)
                return frame_to_observations(
                    iso_name, frame, {"Load": "load"}, "MW", cutoff, fetched_at
                )

            attempt(f"load {day}", load)
        if config.fuel_mix:

            def fuel_mix(day: Any = day) -> list[GridObservation]:
                frame = iso.get_fuel_mix(day)
                columns = {c: f"fuel.{_slug(c)}" for c in frame.columns if c not in TIME_COLUMNS}
                return frame_to_observations(iso_name, frame, columns, "MW", cutoff, fetched_at)

            attempt(f"fuel_mix {day}", fuel_mix)
        prices = config.prices
        if prices is not None:

            def price_rows(day: Any = day, prices: GridPrices = prices) -> list[GridObservation]:
                kwargs: dict[str, Any] = {"market": prices.market}
                if prices.location_type:
                    kwargs["location_type"] = prices.location_type
                frame = getattr(iso, prices.method)(day, **kwargs)
                value_column = "SPP" if "SPP" in frame.columns else "LMP"
                rows = []
                for location in prices.locations:
                    subset = frame[frame["Location"] == location]
                    if subset.empty:
                        errors.append(f"{iso_name} prices {day}: no rows for {location}")
                        continue
                    rows += frame_to_observations(
                        iso_name,
                        subset.reset_index(drop=True),
                        {value_column: f"price.{_slug(location)}"},
                        "$/MWh",
                        cutoff,
                        fetched_at,
                    )
                return rows

            attempt(f"prices {day}", price_rows)
    return observations, errors


class GridCollector:
    name = "gridstatus"

    def __init__(
        self,
        sources: GridSources,
        make_iso: IsoFactory,
        enabled_isos: tuple[str, ...],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sources = sources
        self._make_iso = make_iso
        self._enabled = enabled_isos
        self._clock = clock

    async def collect(self) -> CollectResult:
        now = self._clock()

        def run(iso_name: str) -> tuple[list[GridObservation], list[str]]:
            return collect_iso(
                iso_name,
                self._sources.isos[iso_name],
                self._make_iso(iso_name),
                self._sources.settle_minutes,
                now,
            )

        outcomes = await asyncio.gather(
            *(asyncio.to_thread(run, iso_name) for iso_name in self._enabled)
        )
        result = CollectResult()
        for observations, errors in outcomes:
            result.grid += observations
            result.errors += errors
        return result

    async def aclose(self) -> None:
        return None
