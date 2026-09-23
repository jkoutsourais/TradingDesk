"""Per-collector fetch health for the dashboard's source-health dots."""

from sqlalchemy import Connection, text

MAX_ERROR_LENGTH = 2000


def register_collector(
    conn: Connection, collector: str, *, enabled: bool, disabled_reason: str | None = None
) -> None:
    conn.execute(
        text(
            "INSERT INTO collector_health (collector, enabled, disabled_reason) "
            "VALUES (:collector, :enabled, :reason) "
            "ON CONFLICT (collector) DO UPDATE "
            "SET enabled = EXCLUDED.enabled, disabled_reason = EXCLUDED.disabled_reason"
        ),
        {"collector": collector, "enabled": enabled, "reason": disabled_reason},
    )


def record_success(conn: Connection, collector: str, records_added: int) -> None:
    conn.execute(
        text(
            "UPDATE collector_health SET last_attempt_at = now(), last_success_at = now(), "
            "consecutive_failures = 0, records_added_last = :added, "
            "records_added_total = records_added_total + :added "
            "WHERE collector = :collector"
        ),
        {"collector": collector, "added": records_added},
    )


def record_failure(conn: Connection, collector: str, error: str, records_added: int = 0) -> None:
    conn.execute(
        text(
            "UPDATE collector_health SET last_attempt_at = now(), last_error_at = now(), "
            "last_error = :error, consecutive_failures = consecutive_failures + 1, "
            "records_added_last = :added, records_added_total = records_added_total + :added "
            "WHERE collector = :collector"
        ),
        {"collector": collector, "error": error[:MAX_ERROR_LENGTH], "added": records_added},
    )
