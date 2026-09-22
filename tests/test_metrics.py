import pytest
from sqlalchemy import Connection, text

from desk.metrics import JobStatus, record_job_finish, record_job_start


def fetch_run(db_conn: Connection, run_id: object) -> dict[str, object]:
    row = db_conn.execute(text("SELECT * FROM job_runs WHERE id = :id"), {"id": run_id}).one()
    return dict(row._mapping)


def test_job_run_lifecycle(db_conn: Connection) -> None:
    run_id = record_job_start(db_conn, job="post_market.research", desk="research", model="m")
    running = fetch_run(db_conn, run_id)
    assert running["status"] == "running"
    assert running["finished_at"] is None

    record_job_finish(
        db_conn,
        run_id,
        status=JobStatus.OK,
        tokens_in=1000,
        tokens_out=500,
        load_ms=3000,
        generation_ms=10_000,
    )
    finished = fetch_run(db_conn, run_id)
    assert finished["status"] == "ok"
    assert finished["finished_at"] is not None
    assert finished["tokens_per_s"] == pytest.approx(50.0)


def test_failed_job_records_error(db_conn: Connection) -> None:
    run_id = record_job_start(db_conn, job="collect.finnhub", desk="data")
    record_job_finish(db_conn, run_id, status=JobStatus.FAILED, error="HTTP 429")
    failed = fetch_run(db_conn, run_id)
    assert failed["status"] == "failed"
    assert failed["error"] == "HTTP 429"
    assert failed["tokens_per_s"] is None


def test_failed_job_requires_error(db_conn: Connection) -> None:
    run_id = record_job_start(db_conn, job="collect.edgar", desk="data")
    with pytest.raises(ValueError, match="error"):
        record_job_finish(db_conn, run_id, status=JobStatus.FAILED)


def test_finishing_twice_is_rejected(db_conn: Connection) -> None:
    run_id = record_job_start(db_conn, job="collect.fred", desk="data")
    record_job_finish(db_conn, run_id, status=JobStatus.OK)
    with pytest.raises(ValueError, match="not running"):
        record_job_finish(db_conn, run_id, status=JobStatus.OK)
