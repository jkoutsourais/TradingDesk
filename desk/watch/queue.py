"""Postgres job queue. Workers claim with FOR UPDATE SKIP LOCKED, so concurrent workers
never take the same job, and a dedupe key keeps one job per scheduled slot."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

MAX_ERROR_LENGTH = 2000


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    shift_id: UUID | None


def enqueue(
    conn: Connection,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    dedupe_key: str | None = None,
    run_after: datetime | None = None,
    shift_id: UUID | None = None,
    max_attempts: int = 1,
) -> int | None:
    """Queue a job; returns its id, or None when the dedupe key was already queued."""
    return conn.execute(
        text(
            "INSERT INTO jobs (kind, payload, dedupe_key, run_after, shift_id, max_attempts) "
            "VALUES (:kind, CAST(:payload AS jsonb), :dedupe_key, coalesce(:run_after, now()), "
            ":shift_id, :max_attempts) "
            "ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING RETURNING id"
        ),
        {
            "kind": kind,
            "payload": json.dumps(payload or {}),
            "dedupe_key": dedupe_key,
            "run_after": run_after,
            "shift_id": shift_id,
            "max_attempts": max_attempts,
        },
    ).scalar_one_or_none()


def claim(conn: Connection) -> Job | None:
    row = conn.execute(
        text(
            "UPDATE jobs SET status = 'running', started_at = now(), attempts = attempts + 1 "
            "WHERE id = (SELECT id FROM jobs WHERE status = 'queued' AND run_after <= now() "
            "ORDER BY run_after, id FOR UPDATE SKIP LOCKED LIMIT 1) "
            "RETURNING id, kind, payload, attempts, max_attempts, shift_id"
        )
    ).one_or_none()
    if row is None:
        return None
    return Job(row.id, row.kind, dict(row.payload), row.attempts, row.max_attempts, row.shift_id)


def finish(conn: Connection, job: Job, error: str | None = None) -> str:
    """Mark a job done. A failed job with attempts left goes back to the queue."""
    if error is None:
        status = "ok"
    elif job.attempts < job.max_attempts:
        status = "queued"
    else:
        status = "failed"
    conn.execute(
        text(
            "UPDATE jobs SET status = :status, error = :error, "
            "finished_at = CASE WHEN :status = 'queued' THEN NULL ELSE now() END, "
            "run_after = CASE WHEN :status = 'queued' THEN now() + interval '1 minute' "
            "ELSE run_after END WHERE id = :id"
        ),
        {"status": status, "error": error[:MAX_ERROR_LENGTH] if error else None, "id": job.id},
    )
    return status


def release_stale(conn: Connection) -> int:
    """Jobs left 'running' by a stopped scheduler are failed so their slot is not lost silently."""
    return conn.execute(
        text(
            "UPDATE jobs SET status = 'failed', finished_at = now(), "
            "error = 'interrupted: scheduler restarted' WHERE status = 'running'"
        )
    ).rowcount
