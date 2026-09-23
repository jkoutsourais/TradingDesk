"""Live tastytrade DXLink stream for Tier 0-1: 1-minute candles and latest quotes.

A background task holds the DXLink connection, reconnecting with backoff and at least
every RECONNECT_EVERY_S so futures roll to the new front month and the quote token stays
fresh. The runner calls collect() every minute; it drains completed 1-minute candles and
changed quotes, and raises while the stream is down so the outage shows as failed runs.

DXLink sends 1-minute candles directly, so bars are exact rather than rebuilt from trades.
A candle is final once its minute plus a grace period has passed; later updates to an
already-stored minute are dropped by the price_bars primary key.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tastytrade import DXLinkStreamer
from tastytrade.dxfeed import Candle, Quote
from tastytrade.instruments import Future

from desk.collectors.base import CollectResult, PriceBar, QuoteSnapshot
from desk.collectors.tastytrade_session import TastytradeConnection
from desk.symbols import is_future, to_tastytrade

logger = logging.getLogger(__name__)

CANDLE_GRACE_S = 15
BACKFILL_ON_CONNECT = timedelta(hours=2)
RECONNECT_EVERY_S = 6 * 3600
RETRY_DELAYS_S = (5, 15, 30, 60, 120)
FIRST_CONNECT_WAIT_S = 30


@dataclass(frozen=True, slots=True)
class CandleUpdate:
    symbol: str  # canonical
    minute_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None


class CandleBook:
    """Latest version of each (symbol, minute) candle until the minute is final."""

    def __init__(self) -> None:
        self._pending: dict[tuple[str, int], CandleUpdate] = {}

    def update(self, candle: CandleUpdate) -> None:
        # Zero prices mark empty or removed candles in DXLink snapshots.
        if min(candle.open, candle.high, candle.low, candle.close) <= 0:
            return
        self._pending[(candle.symbol, candle.minute_ms)] = candle

    def drain_completed(self, now: datetime) -> list[PriceBar]:
        cutoff_ms = int(now.timestamp() * 1000) - (60 + CANDLE_GRACE_S) * 1000
        fetched_at = datetime.now(UTC)
        done = [key for key, candle in self._pending.items() if candle.minute_ms <= cutoff_ms]
        bars = []
        for key in sorted(done, key=lambda k: (k[1], k[0])):
            candle = self._pending.pop(key)
            bars.append(
                PriceBar(
                    source="tastytrade",
                    symbol=candle.symbol,
                    interval="1m",
                    ts=datetime.fromtimestamp(candle.minute_ms / 1000, UTC),
                    open=candle.open,
                    high=candle.high,
                    low=candle.low,
                    close=candle.close,
                    volume=candle.volume,
                    fetched_at=fetched_at,
                )
            )
        return bars


class QuoteBook:
    def __init__(self) -> None:
        self._changed: dict[str, QuoteSnapshot] = {}

    def update(self, quote: QuoteSnapshot) -> None:
        self._changed[quote.symbol] = quote

    def drain(self) -> list[QuoteSnapshot]:
        quotes = list(self._changed.values())
        self._changed.clear()
        return quotes


def _base_symbol(event_symbol: str) -> str:
    return event_symbol.split("{", 1)[0]


class TastytradeStream:
    name = "tastytrade_stream"

    def __init__(
        self,
        connection: TastytradeConnection,
        symbols: Callable[[], tuple[str, ...]],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._connection = connection
        self._symbols = symbols
        self._clock = clock
        self._candles = CandleBook()
        self._quotes = QuoteBook()
        self._task: asyncio.Task[None] | None = None
        self._connected = False
        self._last_error: str | None = None
        # Symbols the current connection was built for; a change (a new holding, an edit
        # to tiers.yaml) triggers a reconnect with the new list.
        self._requested: frozenset[str] = frozenset()

    async def _resolve(self) -> dict[str, str]:
        """Map streamer symbol -> canonical symbol, resolving futures to the front month."""
        session = await self._connection.session()
        canonical = self._symbols()
        self._requested = frozenset(canonical)
        mapping = {to_tastytrade(s): s for s in canonical if not is_future(s)}
        roots = [s[1:] for s in canonical if is_future(s)]
        if roots:
            for future in await Future.get(session, product_codes=roots):
                if future.active_month and future.streamer_symbol:
                    mapping[future.streamer_symbol] = f"/{future.product_code}"
        return mapping

    async def _listen_candles(self, streamer: DXLinkStreamer, mapping: dict[str, str]) -> None:
        async for event in streamer.listen(Candle):
            symbol = mapping.get(_base_symbol(event.event_symbol))
            if symbol is None:
                continue
            self._candles.update(
                CandleUpdate(
                    symbol=symbol,
                    minute_ms=event.time,
                    open=Decimal(event.open),
                    high=Decimal(event.high),
                    low=Decimal(event.low),
                    close=Decimal(event.close),
                    volume=event.volume,
                )
            )

    async def _listen_quotes(self, streamer: DXLinkStreamer, mapping: dict[str, str]) -> None:
        async for event in streamer.listen(Quote):
            symbol = mapping.get(event.event_symbol)
            if symbol is None:
                continue
            stamp_ms = max(event.bid_time, event.ask_time)
            self._quotes.update(
                QuoteSnapshot(
                    symbol=symbol,
                    source="tastytrade",
                    bid=event.bid_price or None,
                    ask=event.ask_price or None,
                    bid_size=event.bid_size,
                    ask_size=event.ask_size,
                    # DXLink often sends 0 for quote times; receipt time is the fallback.
                    quote_time=datetime.fromtimestamp(stamp_ms / 1000, UTC)
                    if stamp_ms
                    else self._clock(),
                )
            )

    async def _session_once(self) -> None:
        mapping = await self._resolve()
        session = await self._connection.session()
        async with DXLinkStreamer(session) as streamer:
            await streamer.subscribe(Quote, list(mapping))
            await streamer.subscribe_candle(
                list(mapping),
                "1m",
                start_time=self._clock() - BACKFILL_ON_CONNECT,
                extended_trading_hours=True,
            )
            self._connected = True
            self._last_error = None
            logger.info("tastytrade stream connected for %d symbols", len(mapping))
            async with asyncio.timeout(RECONNECT_EVERY_S):
                async with asyncio.TaskGroup() as group:
                    group.create_task(self._listen_candles(streamer, mapping))
                    group.create_task(self._listen_quotes(streamer, mapping))

    async def _run(self) -> None:
        attempt = 0
        while True:
            started = time.monotonic()
            try:
                await self._session_once()
            except TimeoutError:
                logger.info("tastytrade stream: scheduled reconnect")
                attempt = 0
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any drop means reconnect
                # Logged and surfaced through collect() as failed runs; the loop reconnects.
                self._last_error = f"{type(exc).__name__}: {exc}"[:500]
                logger.warning("tastytrade stream dropped: %s", self._last_error)
            finally:
                self._connected = False
            if time.monotonic() - started > 300:
                attempt = 0
            delay = RETRY_DELAYS_S[min(attempt, len(RETRY_DELAYS_S) - 1)]
            attempt += 1
            await asyncio.sleep(delay)

    async def collect(self) -> CollectResult:
        if (
            self._task is not None
            and self._connected
            and frozenset(self._symbols()) != self._requested
        ):
            logger.info("tastytrade stream: symbol list changed, resubscribing")
            await self.aclose()
            self._task = None
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="tastytrade_stream")
            # Give a fresh connection time to come up so a restart is not logged as a drop.
            deadline = time.monotonic() + FIRST_CONNECT_WAIT_S
            while not self._connected and time.monotonic() < deadline:
                await asyncio.sleep(0.5)
        result = CollectResult(
            bars=self._candles.drain_completed(self._clock()), quotes=self._quotes.drain()
        )
        if not self._connected:
            # Whatever arrived before the drop is still stored; the run is marked failed.
            result.errors.append(f"stream not connected: {self._last_error or 'connecting'}")
        return result

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
