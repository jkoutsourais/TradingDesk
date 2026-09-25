"""Lanes view data: how far each lane candidate got through the pipeline.

Stages, left to right: candidate, promoted (researched), verified (had verified
evidence), thesis, debated, planned, then approved or vetoed. A candidate stops at the
furthest stage it reached; a dropped candidate carries the selection's reason.

    GET /api/lanes                  latest selection's board, funnels and lane metrics
    GET /api/lanes?selection=<id>   the board as it was at an earlier selection
"""

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import Connection, Engine, text

from desk.api.dev import _collector_rows
from desk.config import load_schedule

STAGES = ("candidate", "promoted", "verified", "thesis", "debated", "planned", "approved", "vetoed")
LANES = ("catalyst", "screen", "commodity", "power_grid", "policy", "macro", "event_additions")
# Collectors whose health decides each lane's source dot.
LANE_SOURCES = {
    "catalyst": ("finnhub_company_news", "edgar_latest_filings", "edgar_company_filings"),
    "screen": ("tastytrade_stream", "yahoo_daily_bars", "tastytrade_metrics"),
    "commodity": ("eia", "cftc_cot", "tastytrade_stream"),
    "power_grid": ("gridstatus", "tastytrade_stream"),
    "policy": ("truth_social", "federal_register_documents", "fed_speeches", "fed_press_releases"),
    "macro": ("fred",),
    "event_additions": ("finnhub_earnings_calendar", "edgar_latest_filings"),
}
# Reasons that mean a candidate was never researched.
UNRESEARCHED = ("held", "duplicate", "below the research cut", "an open thesis")
FUNNEL_DAYS = 30
BOARD_CARDS_PER_LANE = 12


def _selections(conn: Connection, limit: int = 40) -> list[dict[str, Any]]:
    return [
        {"id": str(row.id), "created_at": row.created_at.isoformat()}
        for row in conn.execute(
            text(
                "SELECT id, created_at FROM artifacts WHERE kind = 'idea_selection' "
                "ORDER BY created_at DESC LIMIT :n"
            ),
            {"n": limit},
        )
    ]


def _progress(conn: Connection, selection_id: UUID) -> list[dict[str, Any]]:
    """Every candidate of one selection with the furthest stage it reached."""
    selection = conn.execute(
        text("SELECT payload FROM artifacts WHERE id = :id AND kind = 'idea_selection'"),
        {"id": selection_id},
    ).scalar()
    if selection is None:
        raise HTTPException(status_code=404, detail="no such selection")
    selected = set(selection["selected"])
    dropped = {d["candidate_id"]: d["reason"] for d in selection["dropped"]}
    candidates = conn.execute(
        text(
            "SELECT c.id::text AS id, c.payload->>'lane' AS lane, "
            "c.payload->>'instrument' AS instrument, (c.payload->>'score')::float AS score, "
            "c.payload->>'driver' AS driver FROM artifact_parents p "
            "JOIN artifacts c ON c.id = p.parent_id "
            "WHERE p.child_id = :id AND c.kind = 'lane_candidate'"
        ),
        {"id": selection_id},
    ).mappings()
    rows = [dict(c) for c in candidates]
    ids = [r["id"] for r in rows]
    # Theses written from these candidates, then verdicts, plans and decisions on them.
    theses = {
        row.candidate: row.thesis
        for row in conn.execute(
            text(
                "SELECT p.parent_id::text AS candidate, t.id::text AS thesis "
                "FROM artifact_parents p JOIN artifacts t ON t.id = p.child_id "
                "WHERE t.kind = 'thesis' AND t.status = 'ok' AND p.parent_id::text = ANY(:ids)"
            ),
            {"ids": ids},
        )
    }
    thesis_ids = list(theses.values())
    debated = {
        row[0]
        for row in conn.execute(
            text(
                "SELECT DISTINCT root.id::text FROM artifacts root "
                "JOIN artifacts v ON v.kind = 'debate_verdict' "
                "AND (v.payload->>'thesis_id' = root.id::text OR v.payload->>'thesis_id' IN "
                "  (SELECT n.id::text FROM artifacts n WHERE n.kind = 'thesis' "
                "   AND n.payload->>'previous_id' = root.id::text)) "
                "WHERE root.id::text = ANY(:ids)"
            ),
            {"ids": thesis_ids},
        )
    }
    decisions: dict[str, str] = {}
    planned: set[str] = set()
    for row in conn.execute(
        text(
            "SELECT p.payload->>'thesis_id' AS thesis, d.payload->>'decision' AS decision "
            "FROM artifacts p LEFT JOIN artifacts d ON d.kind = 'risk_decision' "
            "AND d.payload->>'plan_id' = p.id::text "
            "WHERE p.kind = 'trade_plan' AND p.payload->>'thesis_id' = ANY(:ids)"
        ),
        {"ids": thesis_ids},
    ):
        planned.add(row.thesis)
        # An approval anywhere in the chain beats a veto.
        if row.decision and decisions.get(row.thesis) != "approved":
            decisions[row.thesis] = "approved" if row.decision != "vetoed" else "vetoed"
    for row in rows:
        reason = dropped.get(row["id"])
        thesis = theses.get(row["id"])
        stage = "candidate"
        if row["id"] in selected or (reason and not reason.startswith(UNRESEARCHED)):
            stage = "promoted"
        if row["id"] in selected or (reason or "").startswith("below the thesis cut"):
            stage = "verified"
        if thesis:
            stage = "thesis"
            if thesis in debated:
                stage = "debated"
            if thesis in planned:
                stage = "planned"
            if thesis in decisions:
                stage = decisions[thesis]
        row.update(
            {
                "stage": stage,
                "dropped": reason,
                "thesis_id": thesis,
            }
        )
    rows.sort(key=lambda r: (-STAGES.index(r["stage"]), -(r["score"] or 0)))
    return rows


def _funnels(conn: Connection, since: datetime) -> dict[str, Any]:
    """Stage counts and drop reasons per lane across every selection since `since`."""
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    drops: dict[str, Counter[str]] = defaultdict(Counter)
    ids = conn.execute(
        text("SELECT id FROM artifacts WHERE kind = 'idea_selection' AND created_at >= :since"),
        {"since": since},
    ).scalars()
    for selection_id in ids:
        for row in _progress(conn, selection_id):
            reached = STAGES.index(row["stage"])
            for stage in STAGES[: min(reached, STAGES.index("planned")) + 1]:
                counts[row["lane"]][stage] += 1
            if row["stage"] in ("approved", "vetoed"):
                counts[row["lane"]][row["stage"]] += 1
            if row["dropped"]:
                drops[row["lane"]][_reason_group(row["dropped"])] += 1
    return {lane: {"stages": dict(counts[lane]), "drops": dict(drops[lane])} for lane in LANES}


def _reason_group(reason: str) -> str:
    for prefix in (
        "held",
        "duplicate",
        "below the research cut",
        "an open thesis",
        "no verified evidence",
        "below the thesis cut",
    ):
        if reason.startswith(prefix):
            return prefix
    return reason[:40]


def _metrics(conn: Connection, since: datetime) -> dict[str, dict[str, Any]]:
    scores = conn.execute(
        text(
            "SELECT payload->'attribution'->>'lane' AS lane, count(*) AS n, "
            "avg(CASE WHEN (payload->>'hit')::boolean THEN 1.0 ELSE 0.0 END) AS hit_rate, "
            "avg((payload->>'return_pct')::float) AS avg_return, "
            "avg((payload->>'r_multiple')::float) AS avg_r "
            "FROM artifacts WHERE kind = 'score' "
            "AND payload->>'subject_kind' IN ('thesis', 'plan') "
            "AND payload->>'horizon' = '5d' AND created_at >= :since GROUP BY 1"
        ),
        {"since": since},
    ).mappings()
    by_lane: dict[str, dict[str, Any]] = {lane: {} for lane in LANES}
    for row in scores:
        if row["lane"] in by_lane:
            by_lane[row["lane"]].update(
                {
                    "scored": row["n"],
                    "hit_rate": row["hit_rate"],
                    "avg_return": row["avg_return"],
                    "avg_r": row["avg_r"],
                }
            )
    # Model work spent on each lane: dossiers and theses descended from its candidates.
    for row in conn.execute(
        text(
            "SELECT c.payload->>'lane' AS lane, count(DISTINCT a.id) AS calls, "
            "sum(a.tokens_in) AS tokens_in, sum(a.tokens_out) AS tokens_out, "
            "sum(a.runtime_ms) AS runtime_ms FROM artifacts c "
            "JOIN artifact_parents p ON p.parent_id = c.id "
            "JOIN artifacts a ON a.id = p.child_id AND a.kind IN ('dossier', 'thesis') "
            "WHERE c.kind = 'lane_candidate' AND c.created_at >= :since GROUP BY 1"
        ),
        {"since": since},
    ).mappings():
        if row["lane"] in by_lane:
            by_lane[row["lane"]].update(
                {
                    "model_calls": row["calls"],
                    "tokens": int((row["tokens_in"] or 0) + (row["tokens_out"] or 0)),
                    "runtime_ms": int(row["runtime_ms"] or 0),
                }
            )
    return by_lane


def _health(conn: Connection) -> dict[str, str]:
    schedule = load_schedule()
    states = {
        c["collector"]: c["state"] for c in _collector_rows(conn, schedule, datetime.now(UTC))
    }
    order = ("failing", "stale", "disabled", "idle", "ok")
    result = {}
    for lane, sources in LANE_SOURCES.items():
        found = [states[s] for s in sources if s in states]
        result[lane] = min(found, key=order.index) if found else "disabled"
    return result


def lanes_router(engine: Engine) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["dashboard"])

    @router.get("/lanes")
    def lanes(selection: UUID | None = None) -> dict[str, Any]:
        with engine.connect() as conn:
            selections = _selections(conn)
            if not selections:
                return {"selections": [], "selection": None, "stages": list(STAGES), "lanes": []}
            chosen = selection or UUID(selections[0]["id"])
            progress = _progress(conn, chosen)
            since = datetime.now(UTC) - timedelta(days=FUNNEL_DAYS)
            funnels = _funnels(conn, since)
            metrics = _metrics(conn, since)
            health = _health(conn)
        lanes_out = []
        for lane in LANES:
            cards = [r for r in progress if r["lane"] == lane]
            lanes_out.append(
                {
                    "lane": lane,
                    "health": health[lane],
                    "candidates": len(cards),
                    "stage_counts": dict(Counter(r["stage"] for r in cards)),
                    "cards": cards[:BOARD_CARDS_PER_LANE],
                    "hidden": max(len(cards) - BOARD_CARDS_PER_LANE, 0),
                    "funnel": funnels[lane],
                    "metrics": metrics[lane],
                }
            )
        return {
            "selections": selections,
            "selection": str(chosen),
            "stages": list(STAGES),
            "lanes": lanes_out,
        }

    return router
