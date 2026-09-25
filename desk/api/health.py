"""Operational detail for the dashboard: collector health, volumes, watch activity and
research dossiers, read from stored rows."""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text

from desk.config import ScheduleConfig

STALE_FACTOR = 2


def collector_rows(conn: Any, schedule: ScheduleConfig, now: datetime) -> list[dict[str, Any]]:
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


def volumes(conn: Any, since: datetime) -> dict[str, Any]:
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


def recent_failures(conn: Any, since: datetime) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            "SELECT job, finished_at, left(error, 300) AS error FROM job_runs "
            "WHERE status = 'failed' AND finished_at >= :since ORDER BY finished_at DESC LIMIT 15"
        ),
        {"since": since},
    ).mappings()
    return [dict(r) for r in rows]


def watch_activity(conn: Any, now: datetime) -> dict[str, Any]:
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


def research_dossiers(conn: Any, now: datetime) -> list[dict[str, Any]]:
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
