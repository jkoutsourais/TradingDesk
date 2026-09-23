"""Watch desk: shifts, job queue, calendar events, tier promotions, trigger cooldowns.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-23
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE shifts (
            id             uuid PRIMARY KEY,
            kind           text NOT NULL,  -- pre_market, briefing, post_market, sunday_futures
            scheduled_for  timestamptz NOT NULL,
            status         text NOT NULL DEFAULT 'scheduled'
                           CHECK (status IN ('scheduled', 'running', 'ok', 'failed', 'skipped')),
            started_at     timestamptz,
            finished_at    timestamptz,
            note           text,
            UNIQUE (kind, scheduled_for)
        )
        """
    )
    op.execute("CREATE INDEX shifts_due_idx ON shifts (status, scheduled_for)")
    # Existing rows all carry NULL shift ids, so the constraints validate immediately.
    op.execute(
        "ALTER TABLE artifacts ADD CONSTRAINT artifacts_shift_fk "
        "FOREIGN KEY (shift_id) REFERENCES shifts (id)"
    )
    op.execute(
        "ALTER TABLE job_runs ADD CONSTRAINT job_runs_shift_fk "
        "FOREIGN KEY (shift_id) REFERENCES shifts (id)"
    )

    # Work queue for the scheduler's workers. Claimed with FOR UPDATE SKIP LOCKED, so two
    # workers never take the same job. dedupe_key stops a restarted scheduler from
    # enqueueing the same time slot twice.
    op.execute(
        """
        CREATE TABLE jobs (
            id            bigserial PRIMARY KEY,
            kind          text NOT NULL,
            payload       jsonb NOT NULL DEFAULT '{}',
            dedupe_key    text,
            run_after     timestamptz NOT NULL DEFAULT now(),
            status        text NOT NULL DEFAULT 'queued'
                          CHECK (status IN ('queued', 'running', 'ok', 'failed')),
            attempts      integer NOT NULL DEFAULT 0,
            max_attempts  integer NOT NULL DEFAULT 1,
            shift_id      uuid REFERENCES shifts (id),
            created_at    timestamptz NOT NULL DEFAULT now(),
            started_at    timestamptz,
            finished_at   timestamptz,
            error         text
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX jobs_dedupe_uq ON jobs (dedupe_key) WHERE dedupe_key IS NOT NULL"
    )
    op.execute("CREATE INDEX jobs_ready_idx ON jobs (run_after) WHERE status = 'queued'")

    # Scheduled releases (FOMC, CPI, jobs from official sources; EIA and COT computed).
    # Operational and refreshed in place, since agencies do move dates.
    op.execute(
        """
        CREATE TABLE calendar_events (
            event_key   text PRIMARY KEY,
            kind        text NOT NULL,
            name        text NOT NULL,
            at          timestamptz NOT NULL,
            source      text NOT NULL,
            updated_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX calendar_events_at_idx ON calendar_events (at)")

    # Event additions: a symbol outside the tiers joins Tier 1 until expires_on.
    op.execute(
        """
        CREATE TABLE tier_promotions (
            symbol       text NOT NULL,
            promoted_at  timestamptz NOT NULL DEFAULT now(),
            expires_on   date NOT NULL,
            reason       text NOT NULL,
            trigger_id   uuid REFERENCES artifacts (id),
            PRIMARY KEY (symbol, promoted_at)
        )
        """
    )
    op.execute("CREATE INDEX tier_promotions_active_idx ON tier_promotions (expires_on)")

    # Cooldown lookups: the latest trigger with a given fingerprint.
    op.execute(
        "CREATE INDEX triggers_fingerprint_idx ON artifacts ((payload->>'fingerprint'), created_at DESC) "
        "WHERE kind = 'trigger'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX triggers_fingerprint_idx")
    op.execute("DROP TABLE tier_promotions")
    op.execute("DROP TABLE calendar_events")
    op.execute("DROP TABLE jobs")
    op.execute("ALTER TABLE job_runs DROP CONSTRAINT job_runs_shift_fk")
    op.execute("ALTER TABLE artifacts DROP CONSTRAINT artifacts_shift_fk")
    op.execute("DROP TABLE shifts")
