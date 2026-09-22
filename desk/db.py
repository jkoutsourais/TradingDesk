"""Database engine construction and programmatic migrations."""

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine

from desk.settings import REPO_ROOT, Settings


def make_engine(settings: Settings) -> Engine:
    # pool_pre_ping survives Postgres container restarts without failing the next job.
    return create_engine(settings.database_url(), pool_pre_ping=True)


def run_migrations(engine: Engine) -> None:
    """Upgrade the database behind `engine` to the latest Alembic revision."""
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    with engine.begin() as connection:
        # env.py uses this connection instead of building its own URL from settings.
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
