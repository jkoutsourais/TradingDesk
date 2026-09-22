"""Test database setup.

Tests that touch Postgres run against a dedicated `<POSTGRES_DB>_test` database that is
dropped and rebuilt from migrations once per session. The append-only triggers block
TRUNCATE and DELETE, so isolation comes from per-test transactions that roll back.
"""

from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, Engine, create_engine, text

from desk.db import run_migrations
from desk.settings import Settings


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    base_settings = Settings()
    test_settings = base_settings.model_copy(
        update={"postgres_db": f"{base_settings.postgres_db}_test"}
    )

    admin_engine = create_engine(
        base_settings.database_url(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    with admin_engine.connect() as admin:
        admin.execute(text(f'DROP DATABASE IF EXISTS "{test_settings.postgres_db}" WITH (FORCE)'))
        admin.execute(text(f'CREATE DATABASE "{test_settings.postgres_db}"'))
    admin_engine.dispose()

    engine = create_engine(test_settings.database_url())
    run_migrations(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_conn(db_engine: Engine) -> Iterator[Connection]:
    with db_engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()
