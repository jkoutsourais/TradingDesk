"""Score rollups for the dashboard: every dimension for one horizon."""

from dataclasses import asdict
from typing import Any

from sqlalchemy import Connection, text

from desk.scoring.rollup import ScoreRow, rollup

DIMENSIONS = (
    "lane",
    "persona",
    "verdict",
    "tier",
    "instrument_class",
    "origin",
    "veto_reason",
    "decision",
    "rating",
    "structure",
)


def score_rows(
    conn: Connection, horizon: str, kinds: tuple[str, ...] | None = None
) -> list[ScoreRow]:
    rows = conn.execute(
        text(
            "SELECT payload->>'subject_kind' AS kind, payload->'attribution' AS attribution, "
            "(payload->>'return_pct')::float AS return_pct, "
            "(payload->>'r_multiple')::float AS r_multiple, "
            "(payload->>'hit')::boolean AS hit FROM artifacts "
            "WHERE kind = 'score' AND payload->>'horizon' = :h"
        ),
        {"h": horizon},
    )
    return [
        ScoreRow(
            {**(row.attribution or {}), "kind": row.kind},
            row.return_pct,
            row.r_multiple,
            row.hit,
        )
        for row in rows
        if kinds is None or row.kind in kinds
    ]


def rollups(conn: Connection, horizon: str) -> dict[str, Any]:
    """Groups per dimension. Personas are scored on their views, verdicts on the judge's
    verdicts, and the veto dimensions on trade plans; lanes and origins on theses."""
    sources = {
        "lane": ("thesis",),
        "persona": ("view",),
        "verdict": ("verdict",),
        "tier": ("plan",),
        "veto_reason": ("plan",),
        "decision": ("plan",),
        "structure": ("plan",),
        "rating": ("rating",),
        "instrument_class": ("thesis", "rating"),
        "origin": ("thesis",),
    }
    all_rows = score_rows(conn, horizon)
    result: dict[str, Any] = {"horizon": horizon, "scored": len(all_rows), "dimensions": {}}
    for dimension in DIMENSIONS:
        kinds = sources[dimension]
        rows = [r for r in all_rows if r.attribution.get("kind") in kinds]
        result["dimensions"][dimension] = [asdict(g) for g in rollup(rows, dimension)]
    return result
