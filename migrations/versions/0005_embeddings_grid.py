"""News embeddings and power-grid observations.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-22
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

# Matches config/models.yaml embedding.dimensions; a model with another width needs a new
# column and a migration, since pgvector indexes require a fixed dimension.
EMBEDDING_DIMENSIONS = 768


def upgrade() -> None:
    # Embeddings follow their raw record: the buffer purge deletes both together.
    op.execute(
        f"""
        CREATE TABLE raw_record_embeddings (
            artifact_id  uuid NOT NULL REFERENCES artifacts (id) ON DELETE CASCADE,
            model        text NOT NULL,
            embedding    vector({EMBEDDING_DIMENSIONS}) NOT NULL,
            created_at   timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (artifact_id, model)
        )
        """
    )
    op.execute(
        "CREATE INDEX raw_record_embeddings_hnsw_idx ON raw_record_embeddings "
        "USING hnsw (embedding vector_cosine_ops)"
    )

    # Grid data as each operator reports it (5- or 15-minute intervals). Only intervals
    # that ended a while ago are stored, so a stored value is final.
    op.execute(
        """
        CREATE TABLE grid_observations (
            iso               text NOT NULL,
            series_id         text NOT NULL,  -- load, fuel.<type>, price.<location>
            interval_start    timestamptz NOT NULL,
            interval_minutes  integer NOT NULL CHECK (interval_minutes > 0),
            value             numeric NOT NULL,
            unit              text NOT NULL,
            fetched_at        timestamptz NOT NULL,
            PRIMARY KEY (iso, series_id, interval_start)
        )
        """
    )
    op.execute(
        "CREATE INDEX grid_observations_recent_idx "
        "ON grid_observations (iso, series_id, interval_start DESC)"
    )
    op.execute(
        "CREATE TRIGGER grid_observations_append_only BEFORE UPDATE OR DELETE "
        "ON grid_observations FOR EACH ROW EXECUTE FUNCTION reject_artifact_mutation()"
    )
    op.execute(
        "CREATE TRIGGER grid_observations_no_truncate BEFORE TRUNCATE ON grid_observations "
        "FOR EACH STATEMENT EXECUTE FUNCTION reject_artifact_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TABLE grid_observations")
    op.execute("DROP TABLE raw_record_embeddings")
