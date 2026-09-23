"""Runtime settings, read from environment variables and the git-ignored `.env` file."""

from pathlib import Path

from pydantic import SecretStr, field_validator
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

    # tastytrade OAuth: client secret of the personal OAuth application and a refresh
    # token from a personal grant. Optional so services that do not touch tastytrade start
    # without them.
    tastytrade_client_secret: SecretStr | None = None
    tastytrade_refresh_token: SecretStr | None = None

    # Data desk sources. A collector whose key is missing is registered as disabled
    # (visible on the dashboard) instead of failing every run.
    finnhub_api_key: SecretStr | None = None
    fred_api_key: SecretStr | None = None
    eia_api_key: SecretStr | None = None
    # SEC fair-access policy: "<name> <contact email>". Stays in .env, never committed.
    sec_user_agent: str | None = None

    # IBKR Flex Web Service (read-only statements); the query ID is the number shown
    # beside the saved Activity Flex Query in Client Portal.
    flex_query_api_token: SecretStr | None = None
    flex_query_id: str | None = None

    # PJM Data Miner (free registration); PJM grid data is skipped until it is set.
    pjm_api_key: SecretStr | None = None

    # ntfy pushes. The topic name is the only access control on hosted ntfy.sh, so it is
    # treated as a secret.
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: SecretStr | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # `KEY=` lines in .env mean "not configured yet", not "configured as empty".
        return None if isinstance(value, str) and not value.strip() else value

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
