"""Prices: append-only price_bars and the mutable quotes_latest snapshot.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-22
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Completed bars only. Collectors never write a bar that is still forming, so a stored
    # bar is final and safe to cite as a data snapshot.
    op.execute(
        """
        CREATE TABLE price_bars (
            source      text NOT NULL,
            symbol      text NOT NULL,
            interval    text NOT NULL CHECK (interval IN ('1m', '1d')),
            ts          timestamptz NOT NULL,
            open        numeric NOT NULL,
            high        numeric NOT NULL,
            low         numeric NOT NULL,
            close       numeric NOT NULL,
            volume      numeric,
            fetched_at  timestamptz NOT NULL,
            PRIMARY KEY (source, symbol, interval, ts),
            CHECK (low <= high)
        )
        """
    )
    op.execute("CREATE INDEX price_bars_symbol_idx ON price_bars (symbol, interval, ts DESC)")
    op.execute(
        "CREATE TRIGGER price_bars_append_only BEFORE UPDATE OR DELETE ON price_bars "
        "FOR EACH ROW EXECUTE FUNCTION reject_artifact_mutation()"
    )
    op.execute(
        "CREATE TRIGGER price_bars_no_truncate BEFORE TRUNCATE ON price_bars "
        "FOR EACH STATEMENT EXECUTE FUNCTION reject_artifact_mutation()"
    )

    # Latest quote per symbol for the dashboard and scanners; overwritten in place.
    # Individual ticks are not stored.
    op.execute(
        """
        CREATE TABLE quotes_latest (
            symbol      text PRIMARY KEY,
            source      text NOT NULL,
            bid         numeric,
            ask         numeric,
            bid_size    numeric,
            ask_size    numeric,
            quote_time  timestamptz NOT NULL,
            updated_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE quotes_latest")
    op.execute("DROP TABLE price_bars")
