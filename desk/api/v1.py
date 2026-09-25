"""JSON API for the dashboard (web/). Every number is read from stored rows.

GET /api/status          shift state, loaded models, last runs, collector health
GET /api/today           latest briefing, open theses, plans, vetoes, ratings
GET /api/desks           per-desk activity summary, problems and source health
GET /api/desks/{id}      desk-specific activity (watch, research)
GET /api/briefs/{id}     one briefing
GET /api/lineage/{id}    an artifact and its ancestors, for the Trace view
GET /api/events          server-sent events: one per new artifact
"""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import Connection, Engine, text

from desk.api.health import (
    collector_rows,
    research_dossiers,
    watch_activity,
)
from desk.artifacts.brief import Brief
from desk.artifacts.store import ArtifactNotFoundError, get_artifact, get_lineage
from desk.config import load_schedule
from desk.desks.analyst.debate import chain_ids, latest_verdicts
from desk.desks.idea.status import open_theses, warning_flags

DESKS = (
    ("data", "Data", "Pulls news, filings, prices and broker statements"),
    ("watch", "Watch", "Scans for moves and news worth a look; runs the shifts"),
    ("research", "Research", "Writes sourced dossiers on flagged subjects"),
    ("factcheck", "Fact-check", "Checks each research claim against its source"),
    ("idea", "Idea", "Ranks candidates by lane and drafts theses"),
    ("holdings", "Holdings", "Rates each current holding every morning"),
    ("analyst", "Analyst", "Debates theses from several analyst views"),
    ("trader", "Trader", "Turns approved ideas into sized trade plans"),
    ("risk", "Risk", "Checks every plan against the risk limits"),
    ("front_office", "Front office", "Writes the briefing and answers chat"),
    ("scoring", "Scoring", "Scores past calls against what prices did"),
)
# Singular and plural names for the outputs counted on the Desks view. Kinds not listed
# (raw records, labels, snapshots) are plumbing and stay off the summary.
OUTPUT_NOUNS = {
    "trigger": ("alert", "alerts"),
    "dossier": ("dossier", "dossiers"),
    "verified_claim": ("claim checked", "claims checked"),
    "idea_selection": ("idea ranking", "idea rankings"),
    "thesis": ("thesis", "theses"),
    "holding_rating": ("holding rated", "holdings rated"),
    "analyst_view": ("analyst view", "analyst views"),
    "debate_verdict": ("debate", "debates"),
    "trade_plan": ("plan", "plans"),
    "risk_decision": ("risk decision", "risk decisions"),
    "brief": ("briefing", "briefings"),
    "chat_reply": ("chat reply", "chat replies"),
    "score": ("score", "scores"),
    "fill": ("fill", "fills"),
    "position": ("position", "positions"),
}
EVENT_POLL_S = 3.0
OLLAMA_TIMEOUT_S = 2.0


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "is_finite"):  # Decimal
        return float(value)
    if hasattr(value, "hex") and hasattr(value, "version"):  # UUID
        return str(value)
    return value


def _rows(conn: Connection, sql: str, **params: Any) -> list[dict[str, Any]]:
    return [_jsonable(dict(row)) for row in conn.execute(text(sql), params).mappings()]


def _shift_state(conn: Connection, now: datetime) -> dict[str, Any]:
    running = _rows(
        conn, "SELECT kind, scheduled_for, started_at FROM shifts WHERE status = 'running'"
    )
    upcoming = _rows(
        conn,
        "SELECT kind, scheduled_for FROM shifts WHERE status = 'scheduled' "
        "AND scheduled_for >= :now ORDER BY scheduled_for LIMIT 1",
        now=now,
    )
    last = _rows(
        conn,
        "SELECT kind, scheduled_for, status, finished_at FROM shifts "
        "WHERE finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1",
    )
    return {
        "running": running[0] if running else None,
        "next": upcoming[0] if upcoming else None,
        "last": last[0] if last else None,
    }


def _loaded_models(base_url: str) -> list[str] | None:
    try:
        with httpx.Client(base_url=base_url, timeout=OLLAMA_TIMEOUT_S) as client:
            models = client.get("/api/ps").json().get("models", [])
    except httpx.HTTPError:
        return None
    return [m["name"] for m in models]


def _brief(conn: Connection, brief_id: UUID | None = None) -> dict[str, Any] | None:
    """One brief, or the latest when no id is given."""
    if brief_id is None:
        brief_id = conn.execute(
            text("SELECT id FROM artifacts WHERE kind = 'brief' ORDER BY created_at DESC LIMIT 1")
        ).scalar()
    if brief_id is None:
        return None
    brief = get_artifact(conn, brief_id)
    assert isinstance(brief, Brief)
    return {
        "id": str(brief.id),
        "created_at": brief.created_at.isoformat(),
        "status": brief.status.value,
        "kind": brief.brief_kind,
        "sections": [{"title": s.title, "lines": list(s.lines)} for s in brief.sections],
    }


def _theses(conn: Connection, tz: Any) -> list[dict[str, Any]]:
    verdicts = latest_verdicts(conn)
    flagged = {f.thesis_id for f in warning_flags(conn, tz)}
    result = []
    for thesis in open_theses(conn):
        verdict = next((verdicts[v] for v in chain_ids(conn, thesis) if v in verdicts), None)
        hard = thesis.invalidation.hard if thesis.invalidation else None
        warning = thesis.invalidation.warning if thesis.invalidation else None
        result.append(
            {
                "id": str(thesis.id),
                "instrument": thesis.primary_instrument,
                "direction": thesis.direction,
                "statement": thesis.statement,
                "origin": thesis.origin,
                "state": thesis.state,
                "conviction": thesis.conviction,
                "review_by": thesis.review_by.isoformat() if thesis.review_by else None,
                "hard": float(hard.level) if hard else None,
                "warning": float(warning.level) if warning else None,
                "evidence": len(thesis.evidence),
                "verdict": verdict[1] if verdict else None,
                "verdict_id": str(verdict[0]) if verdict else None,
                "in_warning_zone": thesis.id in flagged,
                "created_at": thesis.created_at.isoformat(),
            }
        )
    return result


def plans(conn: Connection, since: datetime | None = None, limit: int = 50) -> list[dict[str, Any]]:
    return _rows(
        conn,
        "SELECT p.id, p.created_at, p.payload->>'subject' AS subject, "
        "p.payload->>'structure' AS structure, p.payload->>'instrument' AS instrument, "
        "p.payload->>'direction' AS direction, (p.payload->>'conviction')::int AS conviction, "
        "p.payload->>'thesis_id' AS thesis_id, d.id AS decision_id, "
        "d.payload->>'decision' AS decision, (d.payload->>'size')::int AS size, "
        "(d.payload->>'max_loss')::numeric AS max_loss, (d.payload->>'cost')::numeric AS cost, "
        "(d.payload->>'funding_needed')::numeric AS funding_needed, "
        "d.payload->'veto_reasons'->>0 AS veto, d.payload->>'risk_note' AS risk_note "
        "FROM artifacts p LEFT JOIN artifacts d ON d.kind = 'risk_decision' "
        "AND d.payload->>'plan_id' = p.id::text WHERE p.kind = 'trade_plan' "
        "AND p.created_at >= :since ORDER BY p.created_at DESC LIMIT :n",
        since=since or datetime(2000, 1, 1, tzinfo=UTC),
        n=limit,
    )


def ratings(conn: Connection) -> list[dict[str, Any]]:
    return _rows(
        conn,
        "SELECT DISTINCT ON (payload->>'subject') id, created_at, status, error, "
        "payload->>'subject' AS subject, payload->>'rating' AS rating, "
        "payload->>'previous_rating' AS previous, payload->>'judged_rating' AS judged, "
        "payload->>'confidence_label' AS confidence, payload->>'suggested_action' AS action, "
        "payload->'reasons'->0->>'text' AS reason, payload->'risk_flags' AS flags "
        "FROM artifacts WHERE kind = 'holding_rating' "
        "ORDER BY payload->>'subject', created_at DESC",
    )


def _desks(conn: Connection, now: datetime) -> list[dict[str, Any]]:
    since = now - timedelta(hours=24)
    counts: dict[str, list[dict[str, Any]]] = {}
    for row in _rows(
        conn,
        "SELECT split_part(produced_by, '.', 1) AS prefix, kind, count(*) AS n, "
        "count(*) FILTER (WHERE status = 'failed') AS failed, max(created_at) AS last_at "
        "FROM artifacts WHERE created_at >= :since GROUP BY 1, 2",
        since=since,
    ):
        counts.setdefault(row["prefix"], []).append(row)
    failures = _rows(
        conn,
        "SELECT desk, job, finished_at, left(error, 300) AS error FROM job_runs "
        "WHERE status = 'failed' AND started_at >= :since ORDER BY started_at DESC LIMIT 200",
        since=since,
    )
    desks = []
    for prefix, title, about in DESKS:
        rows = counts.get(prefix, [])
        desk_failures = [f for f in failures if (f["desk"] or "").startswith(prefix)]
        failed_outputs = sum(row["failed"] for row in rows)
        desks.append(
            {
                "id": prefix,
                "title": title,
                "about": about,
                "summary": _activity(rows),
                "problems": failed_outputs + len(desk_failures),
                "last_at": max((row["last_at"] for row in rows), default=None),
                "failures": desk_failures[:20],
            }
        )
    return desks


def _activity(rows: list[dict[str, Any]]) -> list[str]:
    """Plain-language counts of what a desk produced, e.g. "3 theses (1 failed)"."""
    phrases = []
    for row in sorted(rows, key=lambda r: -r["n"]):
        nouns = OUTPUT_NOUNS.get(row["kind"])
        if nouns is None:
            continue
        phrase = f"{row['n']} {nouns[0] if row['n'] == 1 else nouns[1]}"
        if row["failed"]:
            phrase += f" ({row['failed']} failed)"
        phrases.append(phrase)
    return phrases


async def _events(engine: Engine, request: Request) -> AsyncIterator[str]:
    def newest() -> datetime:
        with engine.connect() as conn:
            value = conn.execute(text("SELECT max(created_at) FROM artifacts")).scalar()
        return value or datetime.now(UTC)

    def since(cursor: datetime) -> list[dict[str, Any]]:
        with engine.connect() as conn:
            return _rows(
                conn,
                "SELECT id, kind, status, produced_by, created_at FROM artifacts "
                "WHERE created_at > :c ORDER BY created_at LIMIT 200",
                c=cursor,
            )

    cursor = await asyncio.to_thread(newest)
    yield "retry: 5000\n\n"
    while not await request.is_disconnected():
        rows = await asyncio.to_thread(since, cursor)
        for row in rows:
            yield f"event: artifact\ndata: {json.dumps(row)}\n\n"
        if rows:
            cursor = datetime.fromisoformat(rows[-1]["created_at"])
        else:
            yield ": keep-alive\n\n"
        await asyncio.sleep(EVENT_POLL_S)


def api_router(engine: Engine, ollama_base_url: str) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["dashboard"])
    schedule = load_schedule()

    @router.get("/status")
    def status() -> dict[str, Any]:
        now = datetime.now(UTC)
        with engine.connect() as conn:
            collectors = collector_rows(conn, schedule, now)
            last_job = _rows(
                conn,
                "SELECT job, status, finished_at FROM job_runs WHERE finished_at IS NOT NULL "
                "ORDER BY finished_at DESC LIMIT 1",
            )
            shift = _shift_state(conn, now)
        states = [c["state"] for c in collectors]
        return {
            "now": now.isoformat(),
            "timezone": schedule.timezone,
            "shift": shift,
            "models": _loaded_models(ollama_base_url),
            "last_job": last_job[0] if last_job else None,
            "collectors": {
                "ok": states.count("ok") + states.count("idle"),
                "failing": states.count("failing"),
                "stale": states.count("stale"),
                "disabled": states.count("disabled"),
            },
        }

    @router.get("/today")
    def today() -> dict[str, Any]:
        now = datetime.now(UTC)
        with engine.connect() as conn:
            recent = plans(conn, now - timedelta(days=3))
            return {
                "brief": _brief(conn),
                "theses": _theses(conn, schedule.tz),
                "plans": [p for p in recent if p["decision"] != "vetoed"],
                "vetoes": [p for p in recent if p["decision"] == "vetoed"],
                "ratings": ratings(conn),
            }

    @router.get("/desks")
    def desks() -> dict[str, Any]:
        now = datetime.now(UTC)
        with engine.connect() as conn:
            return {
                "desks": _desks(conn, now),
                "collectors": _jsonable(collector_rows(conn, schedule, now)),
            }

    @router.get("/desks/{desk_id}")
    def desk_detail(desk_id: str) -> dict[str, Any]:
        """Desk-specific activity: watch hits and shifts, research dossiers."""
        now = datetime.now(UTC)
        with engine.connect() as conn:
            if desk_id == "watch":
                return _jsonable(watch_activity(conn, now))
            if desk_id == "research":
                return _jsonable({"dossiers": research_dossiers(conn, now)})
        return {}

    @router.get("/briefs/{brief_id}")
    def brief(brief_id: UUID) -> dict[str, Any]:
        with engine.connect() as conn:
            try:
                found = _brief(conn, brief_id)
            except ArtifactNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        if found is None:
            raise HTTPException(status_code=404, detail="no brief")
        return found

    @router.get("/lineage/{artifact_id}")
    def lineage(artifact_id: UUID) -> list[dict[str, Any]]:
        with engine.connect() as conn:
            try:
                entries = get_lineage(conn, artifact_id)
            except ArtifactNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        return [
            {"depth": entry.depth, "artifact": entry.artifact.model_dump(mode="json")}
            for entry in entries
        ]

    @router.get("/events")
    async def events(request: Request) -> StreamingResponse:
        return StreamingResponse(
            _events(engine, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
