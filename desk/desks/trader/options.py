"""Option candidates from tastytrade: chain metadata plus DXLink quotes and open interest.

Read-only market data. The trader desk asks for a handful of contracts per underlying
(at the money and about five percent out, in one or two expiries around the thesis
horizon) and filters them in code; nothing here places orders.
"""

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Literal, Protocol

import anyio
from tastytrade import DXLinkStreamer
from tastytrade.dxfeed import Greeks, Quote, Summary
from tastytrade.instruments import OptionType, get_option_chain

from desk.collectors.tastytrade_session import TastytradeConnection

logger = logging.getLogger(__name__)

QUOTE_WAIT_S = 8.0
OTM_STEP = 0.05


@dataclass(frozen=True, slots=True)
class OptionQuote:
    symbol: str  # OCC symbol
    underlying: str
    expiry: date
    strike: Decimal
    right: Literal["call", "put"]
    bid: Decimal
    ask: Decimal
    open_interest: int | None
    delta: float | None
    multiplier: int = 100

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> float | None:
        if self.bid <= 0 or self.ask <= 0:
            return None
        return float((self.ask - self.bid) / self.mid * 100)


class OptionSource(Protocol):
    async def candidates(
        self,
        underlying: str,
        right: Literal["call", "put"],
        spot: Decimal,
        horizon_days: int,
        today: date,
    ) -> list[OptionQuote]: ...


def expiry_window(horizon_days: int, today: date) -> tuple[date, date]:
    """Expiries that outlast the horizon without paying for much more time."""
    earliest = today + timedelta(days=max(int(horizon_days * 0.8), 14))
    latest = today + timedelta(days=horizon_days * 2 + 30)
    return earliest, latest


def pick_strikes(strikes: list[Decimal], spot: Decimal, right: str) -> list[Decimal]:
    """At the money and about five percent out of the money."""
    if not strikes:
        return []
    away = spot * Decimal(1 + OTM_STEP if right == "call" else 1 - OTM_STEP)
    chosen = {min(strikes, key=lambda s: abs(s - spot)), min(strikes, key=lambda s: abs(s - away))}
    return sorted(chosen)


class TastytradeOptions:
    def __init__(self, connection: TastytradeConnection) -> None:
        self._connection = connection

    async def candidates(
        self,
        underlying: str,
        right: Literal["call", "put"],
        spot: Decimal,
        horizon_days: int,
        today: date,
    ) -> list[OptionQuote]:
        session = await self._connection.session()
        chain = await get_option_chain(session, underlying)
        earliest, latest = expiry_window(horizon_days, today)
        expiries = sorted(e for e in chain if earliest <= e <= latest)[:2]
        wanted = OptionType.CALL if right == "call" else OptionType.PUT
        contracts = {}
        for expiry in expiries:
            options = [o for o in chain[expiry] if o.option_type is wanted and o.streamer_symbol]
            strikes = pick_strikes([Decimal(o.strike_price) for o in options], spot, right)
            for option in options:
                if Decimal(option.strike_price) in strikes:
                    contracts[option.streamer_symbol] = option
        if not contracts:
            return []
        quotes: dict[str, Quote] = {}
        summaries: dict[str, Summary] = {}
        greeks: dict[str, Greeks] = {}
        async with DXLinkStreamer(session) as streamer:
            symbols = list(contracts)
            await streamer.subscribe(Quote, symbols)
            await streamer.subscribe(Summary, symbols)
            await streamer.subscribe(Greeks, symbols)

            async def collect(event_class: Any, into: dict[str, Any]) -> None:
                event: Any
                async for event in streamer.listen(event_class):
                    into[event.event_symbol] = event
                    if len(into) >= len(symbols):
                        return

            with anyio.move_on_after(QUOTE_WAIT_S):
                async with anyio.create_task_group() as group:
                    group.start_soon(collect, Quote, quotes)
                    group.start_soon(collect, Summary, summaries)
                    group.start_soon(collect, Greeks, greeks)
        result = []
        for streamer_symbol, option in contracts.items():
            quote = quotes.get(streamer_symbol)
            if quote is None or quote.bid_price is None or quote.ask_price is None:
                continue
            summary = summaries.get(streamer_symbol)
            greek = greeks.get(streamer_symbol)
            result.append(
                OptionQuote(
                    symbol=option.symbol,
                    underlying=underlying,
                    expiry=option.expiration_date,
                    strike=Decimal(option.strike_price),
                    right=right,
                    bid=Decimal(str(quote.bid_price)),
                    ask=Decimal(str(quote.ask_price)),
                    open_interest=int(summary.open_interest)
                    if summary and summary.open_interest is not None
                    else None,
                    delta=float(greek.delta) if greek and greek.delta is not None else None,
                    multiplier=int(option.shares_per_contract or 100),
                )
            )
        logger.info(
            "%s %s: %d option quotes of %d contracts",
            underlying,
            right,
            len(result),
            len(contracts),
        )
        return result
