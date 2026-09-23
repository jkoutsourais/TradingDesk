"""Shared types for Data desk collectors."""

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

import httpx

from desk.artifacts.raw_record import RawRecord

USER_AGENT = "AgenticDesk/0.1 (+https://github.com/jkoutsourais/TradingDesk)"
HTTP_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


@dataclass(frozen=True, slots=True)
class SeriesObservation:
    source: str  # "fred", "eia", "cftc_cot"
    series_id: str
    period: date
    value: Decimal
    unit: str | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class PriceBar:
    """A completed OHLCV bar. `ts` is the bar start; daily bars start at midnight ET."""

    source: str  # "yahoo", "tastytrade"
    symbol: str  # canonical desk symbol, e.g. "NVDA", "BRK.B", "/GC", "^VIX"
    interval: str  # "1m" or "1d"
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class QuoteSnapshot:
    symbol: str
    source: str
    bid: Decimal | None
    ask: Decimal | None
    bid_size: Decimal | None
    ask_size: Decimal | None
    quote_time: datetime


@dataclass(frozen=True, slots=True)
class DayStats:
    """Session statistics from DXLink Summary and Trade events for one symbol.

    None means "not in this update", so a Trade (volume only) never clears the open that
    a Summary supplied.
    """

    symbol: str
    day_open: Decimal | None
    day_high: Decimal | None
    day_low: Decimal | None
    day_volume: Decimal | None
    as_of: datetime


@dataclass(frozen=True, slots=True)
class PositionRow:
    symbol: str  # canonical underlying
    contract: str  # the broker's description, unique within one snapshot
    asset_class: str  # equity, option, future, future_option, crypto, other
    quantity: Decimal  # signed: negative is short
    multiplier: Decimal
    avg_cost: Decimal | None  # per unit of the underlying
    cost_basis: Decimal | None
    mark_price: Decimal | None
    market_value: Decimal | None
    currency: str


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    source: str  # ibkr_flex, ibkr_gateway, tastytrade
    broker: str
    account_ref: str  # "<broker>:<last four>"; full numbers never leave the collector
    as_of: datetime
    fetched_at: datetime
    net_liquidation: Decimal | None
    cash: Decimal | None
    settled_cash: Decimal | None
    buying_power: Decimal | None
    currency: str
    positions: tuple[PositionRow, ...]


@dataclass(frozen=True, slots=True)
class RecordEmbedding:
    artifact_id: UUID
    model: str
    vector: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class GridObservation:
    iso: str  # ercot, miso, caiso, pjm
    series_id: str  # load, fuel.<type>, price.<location>
    interval_start: datetime
    interval_minutes: int
    value: Decimal
    unit: str  # MW or $/MWh
    fetched_at: datetime


def account_ref(broker: str, account_number: str) -> str:
    return f"{broker}:{account_number[-4:]}"


@dataclass(slots=True)
class CollectResult:
    records: list[RawRecord] = field(default_factory=list)
    observations: list[SeriesObservation] = field(default_factory=list)
    bars: list[PriceBar] = field(default_factory=list)
    quotes: list[QuoteSnapshot] = field(default_factory=list)
    day_stats: list[DayStats] = field(default_factory=list)
    accounts: list[AccountSnapshot] = field(default_factory=list)
    embeddings: list[RecordEmbedding] = field(default_factory=list)
    grid: list[GridObservation] = field(default_factory=list)
    # Release calendar: events plus the kinds fetched successfully this run, so upcoming
    # events of those kinds that disappeared upstream are removed.
    calendar_events: list[Any] = field(default_factory=list)
    calendar_kinds: list[str] = field(default_factory=list)
    # Partial failures (e.g. one ticker of 35 returned 500). The runner stores what was
    # fetched, then marks the run failed with these messages so nothing fails silently.
    errors: list[str] = field(default_factory=list)


class Collector(Protocol):
    name: str

    async def collect(self) -> CollectResult: ...

    async def aclose(self) -> None: ...


class RateLimiter:
    """Sliding-window limiter: at most `max_calls` acquisitions in any `period` seconds."""

    def __init__(
        self,
        max_calls: int,
        period: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_calls <= 0 or period <= 0:
            raise ValueError("max_calls and period must be positive")
        self._max_calls = max_calls
        self._period = period
        self._clock = clock
        self._sleep = sleep
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                while self._calls and now - self._calls[0] >= self._period:
                    self._calls.popleft()
                if len(self._calls) < self._max_calls:
                    self._calls.append(now)
                    return
                await self._sleep(self._period - (now - self._calls[0]))


def describe_http_error(exc: httpx.HTTPError) -> str:
    """Error text safe to log and store: the query string (API keys) is dropped."""
    try:
        request = exc.request
    except RuntimeError:  # raised by httpx when the error carries no request
        location = ""
    else:
        location = f"{request.method} {request.url.copy_with(query=None)}"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{location} -> HTTP {exc.response.status_code}"
    return f"{location} -> {type(exc).__name__}".strip()


def make_client(
    *,
    base_url: str = "",
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """HTTP client for one collector; tests pass an httpx.MockTransport as `transport`."""
    merged_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    return httpx.AsyncClient(
        base_url=base_url,
        headers=merged_headers,
        params=params,
        timeout=HTTP_TIMEOUT,
        follow_redirects=True,
        transport=transport,
    )
