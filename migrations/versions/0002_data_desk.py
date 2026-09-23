"""Data desk: raw-record indexes and buffer purge, collector health, series observations.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-22
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Dedupe key for the Data desk. Partial so other artifact kinds are unaffected.
    op.execute(
        "CREATE UNIQUE INDEX raw_records_content_hash_uq ON artifacts ((payload->>'content_hash')) "
        "WHERE kind = 'raw_record'"
    )
    op.execute(
        "CREATE INDEX raw_records_source_idx ON artifacts ((payload->>'source'), created_at DESC) "
        "WHERE kind = 'raw_record'"
    )
    op.execute(
        "CREATE INDEX raw_records_tickers_idx ON artifacts USING gin ((payload->'tickers')) "
        "WHERE kind = 'raw_record'"
    )

    # The 7-day news buffer needs one narrow exception to append-only: the purge job may
    # delete raw records, and only after opting in with SET LOCAL desk.purge = 'on'.
    # Raw records cited by any artifact stay protected by the artifact_parents foreign key.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_artifact_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            -- Nested IFs: PL/pgSQL does not short-circuit AND, and OLD.kind only exists
            -- on the artifacts table.
            IF TG_OP = 'DELETE' AND TG_TABLE_NAME = 'artifacts' THEN
                IF OLD.kind = 'raw_record' AND current_setting('desk.purge', true) = 'on' THEN
                    RETURN OLD;
                END IF;
            END IF;
            RAISE EXCEPTION 'append-only: % on % is not allowed', TG_OP, TG_TABLE_NAME
                USING ERRCODE = 'restrict_violation';
        END;
        $$
        """
    )

    # Operational state for the dashboard's source-health dots; updated in place.
    op.execute(
        """
        CREATE TABLE collector_health (
            collector             text PRIMARY KEY,
            enabled               boolean NOT NULL,
            disabled_reason       text,
            last_attempt_at       timestamptz,
            last_success_at       timestamptz,
            last_error_at         timestamptz,
            last_error            text,
            consecutive_failures  integer NOT NULL DEFAULT 0,
            records_added_last    integer NOT NULL DEFAULT 0,
            records_added_total   bigint NOT NULL DEFAULT 0
        )
        """
    )

    # Numeric time series (FRED, EIA, CFTC COT). Kept permanently, outside the news
    # buffer. A revised value arrives as a new row; the latest fetched_at wins.
    op.execute(
        """
        CREATE TABLE series_observations (
            id          bigserial PRIMARY KEY,
            source      text NOT NULL,
            series_id   text NOT NULL,
            period      date NOT NULL,
            value       numeric NOT NULL,
            unit        text,
            fetched_at  timestamptz NOT NULL,
            UNIQUE (source, series_id, period, value)
        )
        """
    )
    op.execute(
        "CREATE INDEX series_observations_lookup_idx "
        "ON series_observations (source, series_id, period DESC, fetched_at DESC)"
    )
    op.execute(
        "CREATE TRIGGER series_observations_append_only BEFORE UPDATE OR DELETE "
        "ON series_observations FOR EACH ROW EXECUTE FUNCTION reject_artifact_mutation()"
    )
    op.execute(
        "CREATE TRIGGER series_observations_no_truncate BEFORE TRUNCATE ON series_observations "
        "FOR EACH STATEMENT EXECUTE FUNCTION reject_artifact_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TABLE series_observations")
    op.execute("DROP TABLE collector_health")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_artifact_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'append-only: % on % is not allowed', TG_OP, TG_TABLE_NAME
                USING ERRCODE = 'restrict_violation';
        END;
        $$
        """
    )
    op.execute("DROP INDEX raw_records_tickers_idx")
    op.execute("DROP INDEX raw_records_source_idx")
    op.execute("DROP INDEX raw_records_content_hash_uq")
