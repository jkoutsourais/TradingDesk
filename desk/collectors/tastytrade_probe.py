"""Report which DXLink market data the tastytrade account is entitled to.

Run: `uv run python -m desk.collectors.tastytrade_probe [--seconds 20]`

Streams a sample of equities, an ETF option and front-month metals/energy futures, then
prints one row per (symbol, event type) showing whether any event arrived and how old
the newest one was. A missing entitlement shows up as silence, not as an error, so a row
marked NO DATA is the signal. During regular hours, ages near 15 minutes on every row
indicate a delayed feed; outside regular hours ages reflect the last trade and are
not meaningful.

Read-only: the script requests a quote token and instrument metadata, nothing else. It
never prints the OAuth secrets, the quote token or account numbers.
"""

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
from pydantic import SecretStr
from tastytrade import DXLinkStreamer, Session
from tastytrade.dxfeed import Candle, Greeks, Quote, Summary, Trade
from tastytrade.instruments import Future, Option, OptionType, get_option_chain

from desk.settings import Settings

logger = logging.getLogger(__name__)

EQUITY_SYMBOLS = ("SPY", "GLD", "SLV", "USO", "XLE")
OPTION_UNDERLYING = "SPY"
FUTURE_PRODUCT_CODES = ("GC", "SI", "CL", "NG")
MIN_OPTION_DAYS = 7


@dataclass
class Observation:
    count: int = 0
    newest_ms: int = 0
    sample: str = ""


@dataclass
class ProbeResult:
    quote_level: str | None = None
    rows: dict[tuple[str, str, str], Observation] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _event_time_ms(event: Any) -> int:
    if isinstance(event, Quote):
        return max(event.bid_time, event.ask_time)
    return int(getattr(event, "time", 0) or 0)


def _event_sample(event: Any) -> str:
    if isinstance(event, Quote):
        return f"bid {event.bid_price} / ask {event.ask_price}"
    if isinstance(event, Trade):
        return f"last {event.price}"
    if isinstance(event, Greeks):
        return f"iv {event.volatility:.3f} delta {event.delta:.3f}"
    if isinstance(event, Candle):
        return f"close {event.close}"
    if isinstance(event, Summary):
        return f"prev close {event.prev_day_close_price} oi {event.open_interest}"
    return ""


async def _front_month_futures(session: Session, result: ProbeResult) -> dict[str, str]:
    """Map product code to the active-month contract's streamer symbol."""
    futures = await Future.get(session, product_codes=list(FUTURE_PRODUCT_CODES))
    front: dict[str, str] = {}
    for future in futures:
        if future.active_month and future.streamer_symbol:
            front[future.product_code] = future.streamer_symbol
    missing = sorted(set(FUTURE_PRODUCT_CODES) - set(front))
    if missing:
        result.notes.append(f"no active-month contract metadata for: {', '.join(missing)}")
    return front


async def _near_atm_option(session: Session, spot: float, result: ProbeResult) -> str | None:
    chain = await get_option_chain(session, OPTION_UNDERLYING)
    cutoff = datetime.now(UTC).date() + timedelta(days=MIN_OPTION_DAYS)
    expiries = sorted(expiry for expiry in chain if expiry >= cutoff)
    if not expiries:
        result.notes.append(f"no {OPTION_UNDERLYING} expiries at least {MIN_OPTION_DAYS} days out")
        return None
    calls = [
        option
        for option in chain[expiries[0]]
        if option.option_type is OptionType.CALL and option.streamer_symbol
    ]
    if not calls:
        return None
    best: Option = min(calls, key=lambda option: abs(float(option.strike_price) - spot))
    return best.streamer_symbol


async def _collect(
    streamer: DXLinkStreamer,
    event_class: type[Any],
    labels: dict[str, tuple[str, str]],
    result: ProbeResult,
    seconds: float,
) -> None:
    with anyio.move_on_after(seconds):
        async for event in streamer.listen(event_class):
            # Candle symbols come back with DXLink attributes, e.g. "SPY{=d,tho=true}".
            base_symbol = event.event_symbol.split("{", 1)[0]
            category, display = labels.get(base_symbol, ("other", event.event_symbol))
            key = (category, display, event_class.__name__)
            observation = result.rows.setdefault(key, Observation())
            observation.count += 1
            event_ms = _event_time_ms(event)
            if event_ms >= observation.newest_ms:
                observation.newest_ms = event_ms
                observation.sample = _event_sample(event)


def _secret(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None and value.get_secret_value() else None


async def _spot_price(streamer: DXLinkStreamer, result: ProbeResult) -> float | None:
    """Mid price of the option underlying, used only to pick a near-the-money strike."""
    await streamer.subscribe(Quote, [OPTION_UNDERLYING])
    with anyio.move_on_after(10):
        quote = await streamer.get_event(Quote)
        # The consumed snapshot still counts as proof the equity quote feed works.
        observation = result.rows.setdefault(("equity", OPTION_UNDERLYING, "Quote"), Observation())
        observation.count += 1
        observation.newest_ms = _event_time_ms(quote)
        observation.sample = _event_sample(quote)
        if quote.bid_price > 0 and quote.ask_price > 0:
            return float((quote.bid_price + quote.ask_price) / 2)
    result.notes.append(f"no usable {OPTION_UNDERLYING} quote; option rows skipped")
    return None


async def run_probe(settings: Settings, seconds: float) -> ProbeResult:
    client_secret = _secret(settings.tastytrade_client_secret)
    refresh_token = _secret(settings.tastytrade_refresh_token)
    if client_secret is None or refresh_token is None:
        raise SystemExit("Set TASTYTRADE_CLIENT_SECRET and TASTYTRADE_REFRESH_TOKEN in .env")

    result = ProbeResult()
    async with Session(provider_secret=client_secret, refresh_token=refresh_token) as session:
        token_info = await session._get("/api-quote-tokens")
        result.quote_level = token_info.get("level")
        futures = await _front_month_futures(session, result)

        async with DXLinkStreamer(session) as streamer:
            spot = await _spot_price(streamer, result)
            option_symbol = await _near_atm_option(session, spot, result) if spot else None

            # streamer symbol -> (category, display name), plus the event types expected.
            labels: dict[str, tuple[str, str]] = {s: ("equity", s) for s in EQUITY_SYMBOLS}
            labels.update({symbol: ("future", f"/{code}") for code, symbol in futures.items()})
            if option_symbol:
                labels[option_symbol] = ("option", option_symbol)
            candle_symbols = [OPTION_UNDERLYING, *futures.values()]

            expected: list[tuple[str, str, str]] = []
            for symbol, (category, display) in labels.items():
                expected += [(category, display, name) for name in ("Quote", "Trade", "Summary")]
                if category == "option":
                    expected.append((category, display, "Greeks"))
                if symbol in candle_symbols:
                    expected.append((category, display, "Candle"))
            for key in expected:
                result.rows.setdefault(key, Observation())

            stream_symbols = list(labels)
            await streamer.subscribe(Quote, stream_symbols)
            await streamer.subscribe(Trade, stream_symbols)
            await streamer.subscribe(Summary, stream_symbols)
            if option_symbol:
                await streamer.subscribe(Greeks, [option_symbol])
            await streamer.subscribe_candle(
                candle_symbols, "1d", start_time=datetime.now(UTC) - timedelta(days=7)
            )

            async with anyio.create_task_group() as group:
                for event_class in (Quote, Trade, Summary, Greeks, Candle):
                    group.start_soon(_collect, streamer, event_class, labels, result, seconds)
    return result


def _format_age(newest_ms: int, now_ms: int) -> str:
    if newest_ms <= 0:
        return "-"
    # Daily candles are stamped slightly ahead of the local clock; clamp instead of going negative.
    age_s = max(0.0, (now_ms - newest_ms) / 1000)
    if age_s < 120:
        return f"{age_s:.0f}s"
    if age_s < 7200:
        return f"{age_s / 60:.0f}m"
    return f"{age_s / 3600:.1f}h"


def print_report(result: ProbeResult) -> None:
    now_ms = int(time.time() * 1000)
    print(f"quote token level: {result.quote_level or 'not reported'}")
    print()
    header = (
        f"{'category':<8} {'symbol':<24} {'event':<8} {'status':<8} "
        f"{'events':>6} {'age':>7}  sample"
    )
    print(header)
    print("-" * len(header))
    order = {"equity": 0, "option": 1, "future": 2, "other": 3}
    for (category, display, event_type), observation in sorted(
        result.rows.items(), key=lambda item: (order.get(item[0][0], 9), item[0][1], item[0][2])
    ):
        status = "ok" if observation.count else "NO DATA"
        age = _format_age(observation.newest_ms, now_ms)
        print(
            f"{category:<8} {display:<24} {event_type:<8} {status:<8} "
            f"{observation.count:>6} {age:>7}  {observation.sample}"
        )
    for note in result.notes:
        print(f"note: {note}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=float, default=20.0, help="listen time per event type")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    # The SDK sets its own logger to DEBUG, which dumps every raw DXLink message.
    logging.getLogger("tastytrade").setLevel(logging.WARNING)
    try:
        result = anyio.run(run_probe, Settings(), args.seconds)
    except KeyboardInterrupt:
        sys.exit(130)
    print_report(result)


if __name__ == "__main__":
    main()
