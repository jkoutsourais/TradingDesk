"""Day statistics on quotes_latest for symbols streamed without 1-minute candles.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-23

DXLink caps 1-minute candle subscriptions (between 100 and 200 symbols per connection),
so Tier 2 streams quotes plus Trade and Summary events, which carry the session's volume,
open, high and low. Those land here, next to the latest quote.
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE quotes_latest
            ALTER COLUMN quote_time DROP NOT NULL,
            ADD COLUMN day_open numeric,
            ADD COLUMN day_high numeric,
            ADD COLUMN day_low numeric,
            ADD COLUMN day_volume numeric,
            ADD COLUMN day_stats_time timestamptz
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE quotes_latest
            DROP COLUMN day_stats_time,
            DROP COLUMN day_volume,
            DROP COLUMN day_low,
            DROP COLUMN day_high,
            DROP COLUMN day_open
        """
    )
