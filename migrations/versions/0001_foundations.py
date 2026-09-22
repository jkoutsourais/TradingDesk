"""Foundations: artifacts, lineage edges, append-only triggers, job_runs, pgvector.

Revision ID: 0001
Revises:
Create Date: 2026-09-22
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Embedding columns arrive with the news buffer; enabling the extension now keeps that
    # migration free of superuser-only statements.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute(
        """
        CREATE TABLE artifacts (
            id              uuid PRIMARY KEY,
            kind            text NOT NULL,
            schema_version  integer NOT NULL,
            status          text NOT NULL CHECK (status IN ('ok', 'failed')),
            error           text,
            shift_id        uuid,
            produced_by     text NOT NULL,
            parents         uuid[] NOT NULL DEFAULT '{}',
            created_at      timestamptz NOT NULL,
            model           text,
            prompt_version  text,
            runtime_ms      integer NOT NULL CHECK (runtime_ms >= 0),
            tokens_in       integer CHECK (tokens_in >= 0),
            tokens_out      integer CHECK (tokens_out >= 0),
            payload         jsonb NOT NULL,
            inserted_at     timestamptz NOT NULL DEFAULT now(),
            CHECK ((status = 'failed') = (error IS NOT NULL))
        )
        """
    )
    op.execute("CREATE INDEX artifacts_kind_created_idx ON artifacts (kind, created_at DESC)")
    op.execute("CREATE INDEX artifacts_shift_idx ON artifacts (shift_id)")
    op.execute("CREATE INDEX artifacts_produced_by_idx ON artifacts (produced_by, created_at DESC)")

    # `artifacts.parents` keeps order for reads; this edge table enforces that every parent
    # exists and gives the Trace view an indexed path for recursive lineage walks.
    op.execute(
        """
        CREATE TABLE artifact_parents (
            child_id   uuid NOT NULL REFERENCES artifacts (id),
            parent_id  uuid NOT NULL REFERENCES artifacts (id),
            ordinal    integer NOT NULL,
            PRIMARY KEY (child_id, parent_id),
            CHECK (child_id <> parent_id)
        )
        """
    )
    op.execute("CREATE INDEX artifact_parents_parent_idx ON artifact_parents (parent_id)")

    op.execute(
        """
        CREATE FUNCTION reject_artifact_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'append-only: % on % is not allowed', TG_OP, TG_TABLE_NAME
                USING ERRCODE = 'restrict_violation';
        END;
        $$
        """
    )
    for table in ("artifacts", "artifact_parents"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_artifact_mutation()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION reject_artifact_mutation()"
        )

    op.execute(
        """
        CREATE TABLE job_runs (
            id             uuid PRIMARY KEY,
            job            text NOT NULL,
            desk           text NOT NULL,
            shift_id       uuid,
            model          text,
            status         text NOT NULL CHECK (status IN ('running', 'ok', 'failed')),
            started_at     timestamptz NOT NULL,
            finished_at    timestamptz,
            load_ms        integer CHECK (load_ms >= 0),
            generation_ms  integer CHECK (generation_ms >= 0),
            tokens_in      integer CHECK (tokens_in >= 0),
            tokens_out     integer CHECK (tokens_out >= 0),
            tokens_per_s   double precision,
            error          text,
            CHECK ((status = 'running') = (finished_at IS NULL))
        )
        """
    )
    op.execute("CREATE INDEX job_runs_started_idx ON job_runs (started_at DESC)")
    op.execute("CREATE INDEX job_runs_desk_idx ON job_runs (desk, started_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE job_runs")
    # The truncate/delete triggers do not fire on DROP TABLE.
    op.execute("DROP TABLE artifact_parents")
    op.execute("DROP TABLE artifacts")
    op.execute("DROP FUNCTION reject_artifact_mutation()")
