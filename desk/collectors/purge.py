"""Seven-day news buffer: delete raw records past retention that no artifact cites."""

from sqlalchemy import Connection, text


def purge_raw_records(conn: Connection, retention_days: int) -> int:
    """Delete expired, uncited raw records and return how many were removed.

    Cited records are skipped explicitly; the artifact_parents foreign key would reject
    their deletion anyway, which is what keeps used claims' sources permanently.
    """
    # SET LOCAL scopes the opt-in to this transaction; the trigger checks it.
    conn.execute(text("SET LOCAL desk.purge = 'on'"))
    deleted = conn.execute(
        text(
            "DELETE FROM artifacts a "
            "WHERE a.kind = 'raw_record' "
            "AND a.created_at < now() - make_interval(days => :days) "
            "AND NOT EXISTS (SELECT 1 FROM artifact_parents e WHERE e.parent_id = a.id)"
        ),
        {"days": retention_days},
    )
    conn.execute(text("SET LOCAL desk.purge = 'off'"))
    return deleted.rowcount
