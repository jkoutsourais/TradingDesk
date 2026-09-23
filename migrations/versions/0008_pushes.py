"""Pushes: every phone notification sent, or held back, and why.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-23

Delivery lives here rather than on the Brief artifact, because artifacts never change
after they are written and a push happens after the brief exists.
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE pushes (
            id          bigserial PRIMARY KEY,
            kind        text NOT NULL CHECK (kind IN ('briefing', 'urgent', 'test')),
            ref_id      uuid REFERENCES artifacts (id),
            title       text NOT NULL,
            message     text NOT NULL,
            click_url   text,
            priority    text NOT NULL,
            status      text NOT NULL CHECK (status IN ('sent', 'held', 'failed')),
            reason      text,
            created_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX pushes_kind_created_idx ON pushes (kind, status, created_at DESC)")
    op.execute(
        "CREATE UNIQUE INDEX pushes_ref_sent_uq ON pushes (kind, ref_id) WHERE status = 'sent'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE pushes")
