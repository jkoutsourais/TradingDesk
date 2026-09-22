"""Runtime settings, read from environment variables and the git-ignored `.env` file."""

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    postgres_user: str
    # SecretStr keeps the password out of reprs, logs and tracebacks.
    postgres_password: SecretStr
    postgres_db: str
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5433

    ollama_base_url: str = "http://localhost:11434"

    api_host: str = "127.0.0.1"
    api_port: int = 8000

    def database_url(self, database: str | None = None) -> URL:
        """SQLAlchemy URL for the psycopg 3 driver; `database` overrides the configured name."""
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=database or self.postgres_db,
        )
