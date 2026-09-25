"""The trader's menu: every way to express one idea, built and priced by code.

Share choices use the subject, its ETF stand-in for futures, the thesis's proxies and
leveraged funds; option choices come from `options.OptionSource`. Each choice carries
its cost and loss per unit at the stop, mapped from the subject's stop by the ratio of
leverage, so the risk desk can size any of them the same way.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

from desk.artifacts.trade import Leg, Structure
from desk.config import CONFIG_DIR
from desk.desks.trader.options import OptionQuote, OptionSource


class TraderConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    future_proxies: dict[str, str]
    levered_proxies: dict[str, tuple[str, ...]]
    default_horizon_days: int
    rating_conviction: dict[str, int]


def load_trader_config(config_dir: Path = CONFIG_DIR) -> TraderConfig:
    with (config_dir / "trader.yaml").open(encoding="utf-8") as handle:
        return TraderConfig.model_validate(yaml.safe_load(handle))


@dataclass(frozen=True, slots=True)
class Choice:
    id: str  # "ch_<n>", what the model picks
    structure: Structure
    instrument: str
    legs: tuple[Leg, ...]
    unit_cost: Decimal
    unit_max_loss: Decimal
    leverage: Decimal
    expiry: date | None = None
    spread_pct: float | None = None
    open_interest: int | None = None
    description: str = ""


@dataclass(frozen=True, slots=True)
class Priced:
    symbol: str
    price: Decimal
    ref: str


def share_structure(symbol: str, stocks: set[str], levered: dict[str, Decimal]) -> Structure:
    if symbol in levered:
        return "levered_etf"
    return "shares" if symbol in stocks else "etf"


def stop_fraction(entry: Decimal, stop: Decimal, direction: str) -> Decimal:
    """Share of the subject's price lost at the stop."""
    move = (entry - stop) if direction == "long" else (stop - entry)
    return move / entry


def share_choice(
    index: int,
    priced: Priced,
    structure: Structure,
    leverage: Decimal,
    subject_leverage: Decimal,
    loss_fraction: Decimal,
    direction: Literal["long", "short"],
) -> Choice:
    unit_loss = (priced.price * loss_fraction * leverage / subject_leverage).quantize(
        Decimal("0.0001")
    )
    leg = Leg(
        action="buy" if direction == "long" else "sell",
        kind="shares",
        symbol=priced.symbol,
        price=priced.price,
        price_ref=priced.ref,
    )
    kind = {"levered_etf": f"{leverage:g}x daily-reset fund", "etf": "ETF", "shares": "shares"}
    return Choice(
        id=f"ch_{index}",
        structure=structure,
        instrument=priced.symbol,
        legs=(leg,),
        unit_cost=priced.price,
        unit_max_loss=max(unit_loss, Decimal("0.0001")),
        leverage=leverage,
        description=f"{priced.symbol} {kind[structure]} at ${priced.price:,.2f}",
    )


def option_leg(quote: OptionQuote, action: Literal["buy", "sell"], price: Decimal) -> Leg:
    return Leg(
        action=action,
        kind="option",
        symbol=quote.symbol,
        expiry=quote.expiry,
        strike=quote.strike,
        right=quote.right,
        price=price,
        price_ref=f"dxlink:quote:{quote.symbol}",
    )


def option_choices(quotes: list[OptionQuote], start: int) -> list[Choice]:
    """A long option per contract, and a debit spread per expiry (buy nearer, sell farther)."""
    choices: list[Choice] = []
    index = start
    for quote in sorted(quotes, key=lambda q: (q.expiry, q.strike)):
        if quote.bid <= 0 or quote.ask <= 0:
            continue
        premium = (quote.mid * quote.multiplier).quantize(Decimal("0.01"))
        structure: Structure = "long_call" if quote.right == "call" else "long_put"
        choices.append(
            Choice(
                id=f"ch_{index}",
                structure=structure,
                instrument=quote.symbol,
                legs=(option_leg(quote, "buy", quote.mid),),
                unit_cost=premium,
                unit_max_loss=premium,
                leverage=Decimal(1),
                expiry=quote.expiry,
                spread_pct=quote.spread_pct,
                open_interest=quote.open_interest,
                description=(
                    f"{quote.underlying} {quote.expiry} {quote.strike:g} {quote.right}, "
                    f"premium ${premium:,.2f} per contract"
                ),
            )
        )
        index += 1
    by_expiry: dict[date, list[OptionQuote]] = {}
    for quote in quotes:
        if quote.bid > 0 and quote.ask > 0:
            by_expiry.setdefault(quote.expiry, []).append(quote)
    for expiry, group in sorted(by_expiry.items()):
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda q: q.strike, reverse=group[0].right == "put")
        near, far = ordered[0], ordered[-1]
        # Buy near the money and sell farther out: pay the ask, receive the bid.
        debit = ((near.ask - far.bid) * near.multiplier).quantize(Decimal("0.01"))
        if debit <= 0:
            continue
        spreads = [q.spread_pct for q in (near, far) if q.spread_pct is not None]
        interest = [q.open_interest for q in (near, far) if q.open_interest is not None]
        choices.append(
            Choice(
                id=f"ch_{index}",
                structure="debit_spread",
                instrument=f"{near.symbol}/{far.symbol}",
                legs=(option_leg(near, "buy", near.ask), option_leg(far, "sell", far.bid)),
                unit_cost=debit,
                unit_max_loss=debit,
                leverage=Decimal(1),
                expiry=expiry,
                spread_pct=max(spreads) if len(spreads) == 2 else None,
                open_interest=min(interest) if len(interest) == 2 else None,
                description=(
                    f"{near.underlying} {expiry} {near.strike:g}/{far.strike:g} {near.right} "
                    f"debit spread, ${debit:,.2f} per spread"
                ),
            )
        )
        index += 1
    return choices


async def build_menu(
    subject: Priced,
    direction: Literal["long", "short"],
    stop: Decimal,
    proxies: list[Priced],
    stocks: set[str],
    levered: dict[str, Decimal],
    options: OptionSource | None,
    horizon_days: int,
    today: date,
    allowed: set[str],
) -> list[Choice]:
    """Choices the account allows; share choices first, then options on the first ETF or stock."""
    loss_fraction = stop_fraction(subject.price, stop, direction)
    if loss_fraction <= 0:
        return []
    subject_leverage = levered.get(subject.symbol, Decimal(1))
    tradeable = [] if subject.symbol.startswith("/") else [subject]
    choices: list[Choice] = []
    for priced in [*tradeable, *proxies]:
        structure = share_structure(priced.symbol, stocks, levered)
        if structure not in allowed or any(c.instrument == priced.symbol for c in choices):
            continue
        if direction == "short" and "short_shares" not in allowed:
            continue
        leverage = levered.get(priced.symbol, Decimal(1))
        choices.append(
            share_choice(
                len(choices) + 1,
                priced,
                structure,
                leverage,
                subject_leverage,
                loss_fraction,
                direction,
            )
        )
    # Options go on the subject or its first unlevered proxy, even when shares of it are
    # not a choice (a short idea in an account that cannot short stock).
    underlying = next((p for p in [*tradeable, *proxies] if p.symbol not in levered), None)
    wants = "long_call" if direction == "long" else "long_put"
    if options is not None and underlying is not None and wants in allowed:
        right: Literal["call", "put"] = "call" if direction == "long" else "put"
        quotes = await options.candidates(
            underlying.symbol, right, underlying.price, horizon_days, today
        )
        for choice in option_choices(quotes, len(choices) + 1):
            if choice.structure in allowed:
                choices.append(choice)
    return choices
