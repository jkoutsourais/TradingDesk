"""Tier 0: symbols held in the latest snapshot of each account, and the position check.

`uv run python -m desk.collectors.holdings` prints each account's latest snapshot per
source so it can be compared with the broker's own app.
"""

import time
from collections.abc import Callable
from typing import Any

from sqlalchemy import Connection, Engine, text

from desk.config import load_schedule
from desk.db import make_engine
from desk.settings import Settings

_LATEST_SNAPSHOTS = (
    "SELECT DISTINCT ON (source, account_ref) id, source, account_ref, as_of, "
    "net_liquidation, cash, settled_cash, buying_power "
    "FROM account_snapshots ORDER BY source, account_ref, as_of DESC"
)
HOLDINGS_CACHE_S = 300


def held_symbols(conn: Connection) -> tuple[str, ...]:
    """Underlyings with a non-zero position in any account's latest snapshot."""
    rows = conn.execute(
        text(
            f"SELECT DISTINCT p.symbol FROM position_snapshots p "  # noqa: S608 - fixed SQL
            f"JOIN ({_LATEST_SNAPSHOTS}) s ON p.snapshot_id = s.id WHERE p.quantity <> 0"
        )
    )
    return tuple(sorted(row[0] for row in rows))


class HeldSymbols:
    """Cached Tier 0 lookup, so per-minute callers do not query on every call."""

    def __init__(self, engine: Engine, clock: Callable[[], float] = time.monotonic) -> None:
        self._engine = engine
        self._clock = clock
        self._symbols: tuple[str, ...] = ()
        self._loaded_at: float | None = None

    def __call__(self) -> tuple[str, ...]:
        now = self._clock()
        if self._loaded_at is None or now - self._loaded_at > HOLDINGS_CACHE_S:
            with self._engine.connect() as conn:
                self._symbols = held_symbols(conn)
            self._loaded_at = now
        return self._symbols


def latest_snapshots(conn: Connection) -> list[dict[str, Any]]:
    snapshots = [dict(row) for row in conn.execute(text(_LATEST_SNAPSHOTS)).mappings()]
    for snapshot in snapshots:
        snapshot["positions"] = [
            dict(row)
            for row in conn.execute(
                text(
                    "SELECT symbol, contract, asset_class, quantity, mark_price, market_value, "
                    "cost_basis FROM position_snapshots WHERE snapshot_id = :id "
                    "ORDER BY symbol, contract"
                ),
                {"id": snapshot["id"]},
            ).mappings()
        ]
    return snapshots


def _money(value: Any) -> str:
    return "-" if value is None else f"{value:,.2f}"


def main() -> None:
    tz = load_schedule().tz
    engine = make_engine(Settings())
    with engine.connect() as conn:
        snapshots = latest_snapshots(conn)
    engine.dispose()
    if not snapshots:
        print("no broker snapshots stored yet")
        return
    for snapshot in snapshots:
        print(
            f"\n{snapshot['account_ref']}  source={snapshot['source']}  "
            f"as of {snapshot['as_of'].astimezone(tz):%Y-%m-%d %H:%M} ET"
        )
        print(
            f"  net liquidation {_money(snapshot['net_liquidation'])}  "
            f"cash {_money(snapshot['cash'])}  settled {_money(snapshot['settled_cash'])}"
        )
        for p in snapshot["positions"]:
            print(
                f"  {p['symbol']:<8} {p['asset_class']:<8} {p['quantity']:>10,.4f}  "
                f"mark {_money(p['mark_price']):>10}  value {_money(p['market_value']):>10}  "
                f"cost {_money(p['cost_basis']):>10}  {p['contract']}"
            )
        if not snapshot["positions"]:
            print("  no open positions")


if __name__ == "__main__":
    main()
