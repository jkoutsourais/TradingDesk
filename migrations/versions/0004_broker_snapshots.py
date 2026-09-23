"""Broker snapshots: account balances and the positions held at each snapshot.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-22

Accounts are identified by broker and the last four digits only; full account numbers
are never stored. Both tables are append-only: a snapshot is the full position set as
of a moment, and a later snapshot supersedes it without changing it.
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE account_snapshots (
            id               bigserial PRIMARY KEY,
            source           text NOT NULL,  -- ibkr_flex, ibkr_gateway, tastytrade
            broker           text NOT NULL,  -- ibkr, tastytrade
            account_ref      text NOT NULL,  -- "<broker>:<last four>"
            as_of            timestamptz NOT NULL,
            fetched_at       timestamptz NOT NULL,
            net_liquidation  numeric,
            cash             numeric,
            settled_cash     numeric,
            buying_power     numeric,
            currency         text NOT NULL,
            UNIQUE (source, account_ref, as_of)
        )
        """
    )
    op.execute(
        "CREATE INDEX account_snapshots_latest_idx "
        "ON account_snapshots (source, account_ref, as_of DESC)"
    )
    op.execute(
        """
        CREATE TABLE position_snapshots (
            snapshot_id   bigint NOT NULL REFERENCES account_snapshots (id),
            symbol        text NOT NULL,  -- canonical underlying, e.g. AEP, BRK.B, /GC
            contract      text NOT NULL,  -- broker's own description of the instrument
            asset_class   text NOT NULL CHECK (
                asset_class IN ('equity', 'option', 'future', 'future_option', 'crypto', 'other')
            ),
            quantity      numeric NOT NULL,
            multiplier    numeric NOT NULL,
            avg_cost      numeric,
            cost_basis    numeric,
            mark_price    numeric,
            market_value  numeric,
            currency      text NOT NULL,
            PRIMARY KEY (snapshot_id, contract)
        )
        """
    )
    op.execute("CREATE INDEX position_snapshots_symbol_idx ON position_snapshots (symbol)")
    for table in ("account_snapshots", "position_snapshots"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_artifact_mutation()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION reject_artifact_mutation()"
        )


def downgrade() -> None:
    op.execute("DROP TABLE position_snapshots")
    op.execute("DROP TABLE account_snapshots")
