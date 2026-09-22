"""Job-run metrics for the Desks tab: runtime, model, tokens, throughput and failures.

Job runs are operational records, not artifacts, so a row is inserted when a job starts
and completed once when it finishes. That lets the dashboard show jobs still in flight.
"""

from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import Connection, text


class JobStatus(StrEnum):
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"


def record_job_start(
    conn: Connection,
    *,
    job: str,
    desk: str,
    shift_id: UUID | None = None,
    model: str | None = None,
) -> UUID:
    run_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO job_runs (id, job, desk, shift_id, model, status, started_at) "
            "VALUES (:id, :job, :desk, :shift_id, :model, 'running', now())"
        ),
        {"id": run_id, "job": job, "desk": desk, "shift_id": shift_id, "model": model},
    )
    return run_id


def record_job_finish(
    conn: Connection,
    run_id: UUID,
    *,
    status: JobStatus,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    load_ms: int | None = None,
    generation_ms: int | None = None,
    error: str | None = None,
) -> None:
    if status is JobStatus.RUNNING:
        raise ValueError("finish status must be ok or failed")
    if status is JobStatus.FAILED and not error:
        raise ValueError("a failed job must record an error")

    tokens_per_s = None
    if tokens_out is not None and generation_ms:
        tokens_per_s = tokens_out / (generation_ms / 1000)

    result = conn.execute(
        text(
            "UPDATE job_runs SET status = :status, finished_at = now(), "
            "tokens_in = :tokens_in, tokens_out = :tokens_out, load_ms = :load_ms, "
            "generation_ms = :generation_ms, tokens_per_s = :tokens_per_s, error = :error "
            "WHERE id = :id AND status = 'running'"
        ),
        {
            "id": run_id,
            "status": status.value,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "load_ms": load_ms,
            "generation_ms": generation_ms,
            "tokens_per_s": tokens_per_s,
            "error": error,
        },
    )
    if result.rowcount != 1:
        raise ValueError(f"job run {run_id} is not running or does not exist")
