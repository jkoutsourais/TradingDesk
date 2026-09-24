"""Thesis state checks, run by code after each close.

Only the hard line (a daily close past it) or the time limit invalidates a thesis; the
warning zone raises a flag for the briefing and, from Phase 6, the bear. A state change
is written as a new Thesis version with the old one as `previous_id`.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, text

from desk.artifacts.store import get_artifact
from desk.artifacts.thesis import FINAL_STATES, Thesis

VERSION_EXCLUDE = frozenset(
    {"id", "created_at", "parents", "previous_id", "change_note", "shift_id", "produced_by"}
    | {"model", "prompt_version", "runtime_ms", "tokens_in", "tokens_out", "status", "error"}
)


def new_version(
    thesis: Thesis, produced_by: str, change_note: str, shift_id: UUID | None = None, **updates: Any
) -> Thesis:
    fields = thesis.model_dump(exclude=set(VERSION_EXCLUDE))
    fields.update(updates)
    evidence = tuple(fields.get("evidence", thesis.evidence))
    return Thesis.model_validate(
        {
            **fields,
            "produced_by": produced_by,
            "runtime_ms": 0,
            "shift_id": shift_id,
            "parents": (thesis.id, *dict.fromkeys(e for e in evidence if e != thesis.id)),
            "previous_id": thesis.id,
            "change_note": change_note,
        }
    )


def open_theses(conn: Connection) -> list[Thesis]:
    """The latest ok version of every thesis chain that is not closed or invalidated."""
    ids = conn.execute(
        text(
            "SELECT t.id FROM artifacts t WHERE t.kind = 'thesis' AND t.status = 'ok' "
            "AND NOT EXISTS (SELECT 1 FROM artifacts n WHERE n.kind = 'thesis' "
            "  AND n.status = 'ok' AND n.payload->>'previous_id' = t.id::text) "
            "ORDER BY t.created_at"
        )
    ).scalars()
    theses = []
    for thesis_id in ids:
        thesis = get_artifact(conn, thesis_id)
        assert isinstance(thesis, Thesis)
        if thesis.state not in FINAL_STATES:
            theses.append(thesis)
    return theses


def latest_close(conn: Connection, symbol: str, tz: ZoneInfo) -> tuple[date, Decimal] | None:
    row = conn.execute(
        text(
            "SELECT ts, close FROM price_bars WHERE source = 'yahoo' AND interval = '1d' "
            "AND symbol = :s ORDER BY ts DESC LIMIT 1"
        ),
        {"s": symbol},
    ).first()
    return (row.ts.astimezone(tz).date(), Decimal(row.close)) if row else None


def latest_price(conn: Connection, symbol: str, tz: ZoneInfo) -> Decimal | None:
    """Live mid when the stream has a two-sided quote, else the last daily close."""
    row = conn.execute(
        text("SELECT bid, ask FROM quotes_latest WHERE symbol = :s"), {"s": symbol}
    ).first()
    if row is not None and row.bid and row.ask and row.bid > 0 and row.ask > 0:
        return (Decimal(row.bid) + Decimal(row.ask)) / 2
    close = latest_close(conn, symbol, tz)
    return close[1] if close else None


def invalidation_note(
    thesis: Thesis, close: tuple[date, Decimal] | None, today: date, tz: ZoneInfo
) -> str | None:
    """Why the thesis is invalidated now, or None when it still stands."""
    invalidation = thesis.invalidation
    if invalidation is None:
        return None
    # Close dates are exchange-local, so compare them with the local creation date.
    if close is not None and close[0] >= thesis.created_at.astimezone(tz).date():
        hard = invalidation.hard
        if hard.crossed(close[1]):
            return (
                f"hard line crossed: {hard.instrument} daily close {close[1]} on {close[0]} "
                f"is {hard.operator} {hard.level}"
            )
    if invalidation.time_limit is not None and today > invalidation.time_limit:
        return f"time limit {invalidation.time_limit} passed"
    return None


@dataclass(frozen=True, slots=True)
class WarningFlag:
    thesis_id: UUID
    instrument: str
    price: Decimal
    level: Decimal
    operator: str


def check_theses(
    conn: Connection, tz: ZoneInfo, today: date, shift_id: UUID | None = None
) -> list[Thesis]:
    """New invalidated versions for open theses that crossed their hard line or time limit."""
    changed = []
    for thesis in open_theses(conn):
        if thesis.state == "draft" or thesis.invalidation is None:
            continue
        close = latest_close(conn, thesis.invalidation.hard.instrument, tz)
        note = invalidation_note(thesis, close, today, tz)
        if note is not None:
            changed.append(new_version(thesis, "idea.status", note, shift_id, state="invalidated"))
    return changed


def warning_flags(conn: Connection, tz: ZoneInfo) -> list[WarningFlag]:
    flags = []
    for thesis in open_theses(conn):
        if thesis.invalidation is None:
            continue
        warning = thesis.invalidation.warning
        price = latest_price(conn, warning.instrument, tz)
        if price is not None and warning.crossed(price):
            flags.append(
                WarningFlag(thesis.id, warning.instrument, price, warning.level, warning.operator)
            )
    return flags
