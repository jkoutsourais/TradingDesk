"""Alembic environment. Migrations are hand-written SQL, so there is no autogenerate metadata."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection

from desk.db import make_engine
from desk.settings import Settings

config = context.config

# Programmatic callers (tests, desk.db.run_migrations) pass their own connection and logging.
if config.config_file_name is not None and "connection" not in config.attributes:
    fileConfig(config.config_file_name)


def run_with_connection(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    provided = config.attributes.get("connection")
    if provided is not None:
        run_with_connection(provided)
        return
    engine = make_engine(Settings())
    try:
        with engine.connect() as connection:
            run_with_connection(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise SystemExit("Offline SQL generation is not supported; run against the database.")
run_migrations_online()
