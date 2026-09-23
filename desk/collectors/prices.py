"""Daily bars from Yahoo and end-of-day volatility metrics from tastytrade.

Yahoo is the single source for daily history (stocks, ETFs, continuous futures, macro
indexes), so every symbol's daily closes come from one place. Only completed bars are
stored: a bar dated today (ET) is still forming, including futures sessions that open at
18:00 ET and carry today's date.
"""

import asyncio
import logging
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from tastytrade.metrics import get_market_metrics
from tastytrade.utils import TastytradeError

from desk.collectors.base import CollectResult, PriceBar, SeriesObservation, describe_http_error
from desk.collectors.tastytrade_session import TastytradeConnection
from desk.symbols import from_tastytrade, is_future, to_tastytrade, to_yahoo

logger = logging.getLogger(__name__)

YAHOO_CHUNK = 100
BACKFILL_PERIOD = "2y"
# One month covers a service outage of up to a few weeks without leaving holes.
UPDATE_PERIOD = "1mo"
BACKFILL_SYMBOLS_PER_RUN = 250
METRICS_CHUNK = 100

Downloader = Callable[[list[str], str], Any]  # returns a pandas DataFrame


def _chunks(items: Sequence[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def yfinance_downloader(cache_dir: Path) -> Downloader:
    import yfinance as yf

    cache_dir.mkdir(parents=True, exist_ok=True)
    # A cache folder owned by this process: the default lives in the account profile, and
    # yfinance's SQLite cache locks when two processes share it.
    yf.set_tz_cache_location(str(cache_dir.resolve()))
    logging.getLogger("yfinance").setLevel(logging.ERROR)

    def download(yahoo_symbols: list[str], period: str) -> Any:
        return yf.download(
            yahoo_symbols,
            period=period,
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=True,
            multi_level_index=True,
        )

    return download


def _price(value: Any) -> Decimal:
    # Yahoo returns float32-derived values; four places keeps cents and sub-dollar ticks.
    return Decimal(f"{float(value):.4f}")


def frame_to_bars(
    frame: Any, yahoo_to_canonical: dict[str, str], today: date, tz: ZoneInfo
) -> tuple[list[PriceBar], set[str]]:
    """Convert a yfinance multi-ticker frame; return bars and the symbols that had none."""
    fetched_at = datetime.now(UTC)
    bars: list[PriceBar] = []
    empty: set[str] = set()
    tickers = set(frame.columns.get_level_values(0)) if not frame.empty else set()
    for yahoo_symbol, canonical in yahoo_to_canonical.items():
        if yahoo_symbol not in tickers:
            empty.add(canonical)
            continue
        rows = frame[yahoo_symbol].dropna(subset=["Open", "High", "Low", "Close"])
        if rows.empty:
            empty.add(canonical)
            continue
        for index, row in rows.iterrows():
            bar_date = index.date()
            if bar_date >= today:
                continue
            volume = row.get("Volume")
            bars.append(
                PriceBar(
                    source="yahoo",
                    symbol=canonical,
                    interval="1d",
                    ts=datetime.combine(bar_date, time(0), tzinfo=tz).astimezone(UTC),
                    open=_price(row["Open"]),
                    high=_price(row["High"]),
                    low=_price(row["Low"]),
                    close=_price(row["Close"]),
                    volume=Decimal(int(volume))
                    if volume == volume and volume is not None
                    else None,
                    fetched_at=fetched_at,
                )
            )
    return bars, empty


class YahooDailyBars:
    name = "yahoo_daily_bars"

    def __init__(
        self,
        symbols: Callable[[], tuple[str, ...]],
        latest_bar_dates: Callable[[], dict[str, date]],
        download: Downloader,
        tz: ZoneInfo,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._symbols = symbols
        self._latest_bar_dates = latest_bar_dates
        self._download = download
        self._tz = tz
        self._clock = clock

    async def collect(self) -> CollectResult:
        return await asyncio.to_thread(self._collect_sync)

    def _fetch(
        self, canonical: list[str], period: str, today: date
    ) -> tuple[list[PriceBar], set[str]]:
        mapping = {to_yahoo(symbol): symbol for symbol in canonical}
        bars, empty = frame_to_bars(self._download(list(mapping), period), mapping, today, self._tz)
        if empty:
            # yfinance occasionally drops tickers on a cold cache or a transient error;
            # one retry of just those symbols clears nearly all of them.
            retry_map = {to_yahoo(symbol): symbol for symbol in sorted(empty)}
            retry_bars, empty = frame_to_bars(
                self._download(list(retry_map), period), retry_map, today, self._tz
            )
            bars += retry_bars
        return bars, empty

    def _collect_sync(self) -> CollectResult:
        today = self._clock().astimezone(self._tz).date()
        known = self._latest_bar_dates()
        symbols = self._symbols()
        backfill = [s for s in symbols if s not in known][:BACKFILL_SYMBOLS_PER_RUN]
        update = [s for s in symbols if s in known]
        result = CollectResult()
        for period, group in ((BACKFILL_PERIOD, backfill), (UPDATE_PERIOD, update)):
            for chunk in _chunks(group, YAHOO_CHUNK):
                bars, empty = self._fetch(chunk, period, today)
                result.bars += bars
                if empty:
                    result.errors.append(f"no Yahoo data for: {', '.join(sorted(empty))}")
        if backfill:
            logger.info(
                "backfilled %s daily history for %d symbols", BACKFILL_PERIOD, len(backfill)
            )
        return result

    async def aclose(self) -> None:
        return None


# Metric field -> unit. Values are copied as tastytrade reports them.
METRIC_FIELDS: dict[str, str] = {
    "implied_volatility_index": "ratio",
    "implied_volatility_index_5_day_change": "ratio",
    "tw_implied_volatility_index_rank": "ratio",
    "implied_volatility_percentile": "ratio",
    "implied_volatility_30_day": "percent",
    "historical_volatility_30_day": "percent",
    "historical_volatility_90_day": "percent",
    "liquidity_rating": "rating",
}


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


class TastytradeMetrics:
    """Daily IV index, IV rank and percentile, IV30, HV and liquidity per symbol.

    Recording these every trading day builds the IV history that IV rank needs, and the
    liquidity rating feeds the Tier 2 options-liquidity ranking later.
    """

    name = "tastytrade_metrics"

    def __init__(
        self,
        connection: TastytradeConnection,
        symbols: Callable[[], tuple[str, ...]],
        tz: ZoneInfo,
    ) -> None:
        self._connection = connection
        self._symbols = symbols
        self._tz = tz

    async def collect(self) -> CollectResult:
        session = await self._connection.session()
        result = CollectResult()
        period = datetime.now(self._tz).date()
        fetched_at = datetime.now(UTC)
        requested = [to_tastytrade(s) if not is_future(s) else s for s in self._symbols()]
        returned: set[str] = set()
        for chunk in _chunks(requested, METRICS_CHUNK):
            try:
                metrics = await get_market_metrics(session, chunk)
            except httpx.HTTPError as exc:
                result.errors.append(f"metrics chunk {chunk[0]}..: {describe_http_error(exc)}")
                continue
            except TastytradeError as exc:
                result.errors.append(f"metrics chunk {chunk[0]}..: {exc}")
                continue
            for info in metrics:
                symbol = from_tastytrade(info.symbol)
                returned.add(info.symbol)
                for field, unit in METRIC_FIELDS.items():
                    value = _to_decimal(getattr(info, field, None))
                    if value is None:
                        continue
                    result.observations.append(
                        SeriesObservation(
                            source="tastytrade_metrics",
                            series_id=f"{symbol}.{field}",
                            period=period,
                            value=value,
                            unit=unit,
                            fetched_at=fetched_at,
                        )
                    )
        missing = sorted(set(requested) - returned)
        if missing:
            # Reported, not failed: some listings (new IPOs, some ETFs) have no metrics.
            logger.info(
                "no tastytrade metrics for %d symbols: %s", len(missing), ", ".join(missing[:20])
            )
        return result

    async def aclose(self) -> None:
        return None
