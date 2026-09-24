"""Temporary development status page: GET /dev (HTML) and GET /dev/status (JSON).

Shows collector health, data volumes, holdings, quotes, recent failures and recent
commits so progress can be followed remotely. It is read-only and is replaced by the
Phase 9 dashboard; access control is enforced by the proxy in front of it.
"""

import subprocess
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from sqlalchemy import Engine, text

from desk.collectors.holdings import latest_snapshots
from desk.config import ScheduleConfig, load_schedule
from desk.desks.idea.status import open_theses
from desk.settings import REPO_ROOT

QUOTE_SYMBOLS = ("SPY", "QQQ", "GLD", "SLV", "/GC", "/SI", "/CL", "/NG", "XLE", "XLU")
STALE_FACTOR = 2


def _collector_rows(conn: Any, schedule: ScheduleConfig, now: datetime) -> list[dict[str, Any]]:
    rows = []
    for row in conn.execute(text("SELECT * FROM collector_health ORDER BY collector")).mappings():
        item = dict(row)
        config = schedule.collectors.get(item["collector"])
        state = "disabled"
        if item["enabled"] and config is not None:
            last = item["last_success_at"]
            window_closed = config.window is not None and not config.window.contains(
                now, schedule.tz
            )
            if item["consecutive_failures"]:
                state = "failing"
            elif window_closed:
                state = "idle"
            elif (
                last is None
                or (now - last).total_seconds() > STALE_FACTOR * config.interval_seconds
            ):
                state = "stale"
            else:
                state = "ok"
        item["state"] = state
        item["interval_seconds"] = config.interval_seconds if config else None
        rows.append(item)
    return rows


def _scalar(conn: Any, sql: str, **params: Any) -> Any:
    return conn.execute(text(sql), params).scalar_one()


def _volumes(conn: Any, since: datetime) -> dict[str, Any]:
    by_source = conn.execute(
        text(
            "SELECT payload->>'source' AS source, count(*) AS n FROM artifacts "
            "WHERE kind = 'raw_record' AND created_at >= :since GROUP BY 1 ORDER BY 2 DESC"
        ),
        {"since": since},
    ).mappings()
    return {
        "raw_records_24h": [dict(r) for r in by_source],
        "raw_records_total": _scalar(
            conn, "SELECT count(*) FROM artifacts WHERE kind = 'raw_record'"
        ),
        "embeddings_total": _scalar(conn, "SELECT count(*) FROM raw_record_embeddings"),
        "daily_bars_total": _scalar(conn, "SELECT count(*) FROM price_bars WHERE interval = '1d'"),
        "daily_bar_symbols": _scalar(
            conn, "SELECT count(DISTINCT symbol) FROM price_bars WHERE interval = '1d'"
        ),
        "minute_bars_24h": _scalar(
            conn,
            "SELECT count(*) FROM price_bars WHERE interval = '1m' AND ts >= :since",
            since=since,
        ),
        "series_observations_24h": _scalar(
            conn, "SELECT count(*) FROM series_observations WHERE fetched_at >= :since", since=since
        ),
        "grid_observations_24h": _scalar(
            conn, "SELECT count(*) FROM grid_observations WHERE fetched_at >= :since", since=since
        ),
    }


def _quotes(conn: Any, symbols: list[str]) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            "SELECT q.symbol, q.bid, q.ask, q.quote_time, "
            "(SELECT close FROM price_bars b WHERE b.symbol = q.symbol AND b.interval = '1d' "
            " ORDER BY ts DESC LIMIT 1) AS prev_close "
            "FROM quotes_latest q WHERE q.symbol = ANY(:symbols) ORDER BY q.symbol"
        ),
        {"symbols": symbols},
    ).mappings()
    return [dict(r) for r in rows]


def _recent_failures(conn: Any, since: datetime) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            "SELECT job, finished_at, left(error, 300) AS error FROM job_runs "
            "WHERE status = 'failed' AND finished_at >= :since ORDER BY finished_at DESC LIMIT 15"
        ),
        {"since": since},
    ).mappings()
    return [dict(r) for r in rows]


def _watch(conn: Any, now: datetime) -> dict[str, Any]:
    triggers = conn.execute(
        text(
            "SELECT created_at, payload->>'rule_id' AS rule, payload->>'instrument' AS instrument, "
            "payload->>'tier' AS tier, (payload->>'importance')::float AS importance, "
            "(payload->>'urgent')::boolean AS urgent, payload->>'summary' AS summary "
            "FROM artifacts WHERE kind = 'trigger' AND created_at >= :since "
            "ORDER BY created_at DESC LIMIT 40"
        ),
        {"since": now - timedelta(hours=24)},
    ).mappings()
    shifts = conn.execute(
        text(
            "(SELECT kind, scheduled_for, status, note FROM shifts WHERE scheduled_for < :now "
            " ORDER BY scheduled_for DESC LIMIT 4) UNION ALL "
            "(SELECT kind, scheduled_for, status, note FROM shifts WHERE scheduled_for >= :now "
            " ORDER BY scheduled_for LIMIT 5) ORDER BY scheduled_for"
        ),
        {"now": now},
    ).mappings()
    queue = conn.execute(
        text("SELECT status, count(*) AS n FROM jobs WHERE created_at >= :since GROUP BY status"),
        {"since": now - timedelta(hours=1)},
    ).mappings()
    promotions = conn.execute(
        text(
            "SELECT symbol, expires_on, reason FROM tier_promotions "
            "WHERE expires_on >= :today ORDER BY promoted_at DESC"
        ),
        {"today": now.date()},
    ).mappings()
    events = conn.execute(
        text("SELECT kind, name, at FROM calendar_events WHERE at >= :now ORDER BY at LIMIT 6"),
        {"now": now},
    ).mappings()
    return {
        "triggers": [dict(r) for r in triggers],
        "shifts": [dict(r) for r in shifts],
        "queue_last_hour": {r["status"]: r["n"] for r in queue},
        "promotions": [dict(r) for r in promotions],
        "calendar": [dict(r) for r in events],
    }


def evidence_score(entailments: list[float | None], claims: int) -> float | None:
    """Mean support over all claims in a dossier, counting unaccepted claims as zero.

    `entailments` holds one entry per accepted (verified or corrected) claim. A dossier with
    no claims has no evidence score rather than a perfect one.
    """
    if claims == 0:
        return None
    return round(sum(e or 0.0 for e in entailments) / claims, 2)


def _research(conn: Any, now: datetime) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            "SELECT DISTINCT ON (d.payload->>'subject') d.id, d.created_at, d.status, d.error, "
            "d.payload->>'subject' AS subject, d.payload->>'subject_kind' AS subject_kind, "
            "(d.payload->>'selection_score')::float AS score, d.payload->'claim_ids' AS claim_ids, "
            "d.payload->'sections'->0->>'text' AS why "
            "FROM artifacts d WHERE d.kind = 'dossier' AND d.created_at >= :since "
            "ORDER BY d.payload->>'subject', d.created_at DESC"
        ),
        {"since": now - timedelta(hours=36)},
    ).mappings()
    dossiers = []
    for row in rows:
        claim_ids = [str(c) for c in row["claim_ids"] or []]
        verdicts = conn.execute(
            text(
                "SELECT DISTINCT ON (payload->>'claim_id') payload->>'verdict' AS verdict, "
                "(payload->>'entailment')::float AS entailment FROM artifacts "
                "WHERE kind = 'verified_claim' AND status = 'ok' "
                "AND payload->>'claim_id' = ANY(:ids) "
                "ORDER BY payload->>'claim_id', created_at DESC"
            ),
            {"ids": claim_ids},
        ).all()
        tally = {"verified": 0, "corrected": 0, "rejected": 0}
        for verdict in verdicts:
            tally[verdict.verdict] += 1
        accepted = [v.entailment for v in verdicts if v.verdict != "rejected"]
        dossiers.append(
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "status": row["status"],
                "error": row["error"],
                "subject": row["subject"],
                "subject_kind": row["subject_kind"],
                "score": row["score"],
                "why": row["why"] if row["subject_kind"] == "play" else None,
                "claims": len(claim_ids),
                "pending": len(claim_ids) - len(verdicts),
                **tally,
                "evidence": evidence_score(accepted, len(claim_ids)),
            }
        )
    dossiers.sort(key=lambda d: (d["subject_kind"] != "holding", -(d["score"] or 0), d["subject"]))
    return dossiers


def _ideas(conn: Any) -> dict[str, Any]:
    """The latest shift's lane funnel and every open thesis."""
    selection = conn.execute(
        text(
            "SELECT id, created_at, payload FROM artifacts WHERE kind = 'idea_selection' "
            "ORDER BY created_at DESC LIMIT 1"
        )
    ).first()
    funnel: list[dict[str, Any]] = []
    if selection is not None:
        dropped = {d["candidate_id"]: d["reason"] for d in selection.payload["dropped"]}
        selected = set(selection.payload["selected"])
        rows = conn.execute(
            text(
                "SELECT c.id::text AS id, c.payload->>'lane' AS lane, "
                "c.payload->>'instrument' AS instrument, (c.payload->>'score')::float AS score, "
                "c.payload->>'driver' AS driver FROM artifact_parents p "
                "JOIN artifacts c ON c.id = p.parent_id WHERE p.child_id = :id "
                "ORDER BY (c.payload->>'score')::float DESC"
            ),
            {"id": selection.id},
        ).mappings()
        funnel = [
            {
                **row,
                "outcome": "thesis" if row["id"] in selected else dropped.get(row["id"], "-"),
            }
            for row in rows
        ]
    theses = [
        {
            "id": t.id,
            "created_at": t.created_at,
            "instrument": t.primary_instrument,
            "origin": t.origin,
            "direction": t.direction,
            "state": t.state,
            "conviction": t.conviction,
            "review_by": t.review_by,
            "hard": t.invalidation.hard.level if t.invalidation else None,
            "warning": t.invalidation.warning.level if t.invalidation else None,
            "evidence": len(t.evidence),
            "statement": t.statement,
        }
        for t in open_theses(conn)
    ]
    return {
        "selection_at": selection.created_at if selection is not None else None,
        "funnel": funnel,
        "theses": theses,
    }


def _analyst(conn: Any) -> dict[str, Any]:
    """Latest holding ratings and the most recent debate verdicts."""
    ratings = conn.execute(
        text(
            "SELECT DISTINCT ON (payload->>'subject') id, created_at, status, error, "
            "payload->>'subject' AS subject, payload->>'rating' AS rating, "
            "payload->>'previous_rating' AS previous, payload->>'judged_rating' AS judged, "
            "payload->>'confidence_label' AS confidence, payload->'risk_flags' AS flags, "
            "payload->>'suggested_action' AS action FROM artifacts "
            "WHERE kind = 'holding_rating' ORDER BY payload->>'subject', created_at DESC"
        )
    ).mappings()
    verdicts = conn.execute(
        text(
            "SELECT id, created_at, payload->>'subject' AS subject, "
            "payload->>'thesis_id' AS thesis_id, payload->>'verdict' AS verdict, "
            "payload->>'confidence_label' AS confidence, "
            "payload->>'conviction_label' AS conviction, payload->'rubric' AS rubric "
            "FROM artifacts WHERE kind = 'debate_verdict' ORDER BY created_at DESC LIMIT 20"
        )
    ).mappings()
    return {
        "ratings": [{**row, "flags": [f["detail"] for f in row["flags"] or []]} for row in ratings],
        "verdicts": [dict(row) for row in verdicts],
    }


def _commits() -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "log", "-8", "--format=%h %ad %s", "--date=short"],  # noqa: S607
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return completed.stdout.splitlines()


def build_status(engine: Engine, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    schedule = load_schedule()
    with engine.connect() as conn:
        snapshots = latest_snapshots(conn)
        held = sorted({p["symbol"] for s in snapshots for p in s["positions"]})
        return {
            "generated_at": now,
            "timezone": schedule.timezone,
            "collectors": _collector_rows(conn, schedule, now),
            "volumes": _volumes(conn, now - timedelta(hours=24)),
            "holdings": snapshots,
            "quotes": _quotes(conn, sorted({*held, *QUOTE_SYMBOLS})),
            "failures": _recent_failures(conn, now - timedelta(hours=6)),
            "watch": _watch(conn, now),
            "research": _research(conn, now),
            "ideas": _ideas(conn),
            "analyst": _analyst(conn),
            "commits": _commits(),
        }


def dev_router(engine: Engine) -> APIRouter:
    router = APIRouter()
    page = resources.files("desk.api").joinpath("dev.html").read_text(encoding="utf-8")

    @router.get("/dev", response_class=HTMLResponse, include_in_schema=False)
    def dev_page() -> str:
        return page

    @router.get("/dev/status")
    def dev_status() -> dict[str, Any]:
        return build_status(engine)

    return router
