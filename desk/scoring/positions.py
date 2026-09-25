"""Fills to positions: FIFO round trips per account and contract.

A position opens with the first fill from flat and closes when the quantity returns to
zero; the next fill starts a new position. Realized P&L matches closing quantity
against the oldest open lots, times the contract multiplier, less every fee.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True, slots=True)
class FillRow:
    exec_id: str
    account_ref: str
    symbol: str  # underlying
    contract: str  # what was traded
    side: Literal["buy", "sell"]
    quantity: Decimal  # positive
    price: Decimal
    multiplier: Decimal
    fees: Decimal  # positive cost
    executed_at: datetime


@dataclass
class Built:
    account_ref: str
    symbol: str
    contract: str
    direction: Literal["long", "short"]
    fills: list[FillRow] = field(default_factory=list)
    quantity: Decimal = Decimal(0)  # signed open quantity
    max_quantity: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    entry_value: Decimal = Decimal(0)
    entry_quantity: Decimal = Decimal(0)
    exit_value: Decimal = Decimal(0)
    exit_quantity: Decimal = Decimal(0)
    opened_at: datetime | None = None
    closed_at: datetime | None = None

    @property
    def state(self) -> Literal["open", "closed"]:
        return "closed" if self.quantity == 0 else "open"

    @property
    def avg_entry(self) -> Decimal | None:
        return self.entry_value / self.entry_quantity if self.entry_quantity else None

    @property
    def avg_exit(self) -> Decimal | None:
        return self.exit_value / self.exit_quantity if self.exit_quantity else None

    @property
    def holding_days(self) -> int | None:
        if self.opened_at is None:
            return None
        end = self.closed_at
        return (end - self.opened_at).days if end else None


def build_positions(fills: list[FillRow]) -> list[Built]:
    by_contract: dict[tuple[str, str], list[FillRow]] = defaultdict(list)
    for fill in sorted(fills, key=lambda f: (f.executed_at, f.exec_id)):
        by_contract[(fill.account_ref, fill.contract)].append(fill)
    positions: list[Built] = []
    for (account, contract), rows in by_contract.items():
        current: Built | None = None
        lots: list[list[Decimal]] = []  # [quantity, price] of open lots, oldest first
        for fill in rows:
            signed = fill.quantity if fill.side == "buy" else -fill.quantity
            if current is None:
                current = Built(
                    account,
                    fill.symbol,
                    contract,
                    "long" if signed > 0 else "short",
                    opened_at=fill.executed_at,
                )
                positions.append(current)
                lots = []
            current.fills.append(fill)
            current.realized_pnl -= fill.fees
            opening = (signed > 0) == (current.direction == "long")
            if opening:
                lots.append([fill.quantity, fill.price])
                current.entry_value += fill.quantity * fill.price
                current.entry_quantity += fill.quantity
            else:
                remaining = fill.quantity
                sign = Decimal(1) if current.direction == "long" else Decimal(-1)
                while remaining > 0 and lots:
                    lot = lots[0]
                    matched = min(remaining, lot[0])
                    current.realized_pnl += sign * (fill.price - lot[1]) * matched * fill.multiplier
                    lot[0] -= matched
                    remaining -= matched
                    if lot[0] == 0:
                        lots.pop(0)
                current.exit_value += fill.quantity * fill.price
                current.exit_quantity += fill.quantity
            current.quantity += signed
            current.max_quantity = max(current.max_quantity, abs(current.quantity))
            if current.quantity == 0:
                current.closed_at = fill.executed_at
                current = None
    return positions
