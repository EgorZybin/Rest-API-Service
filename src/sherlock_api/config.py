from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = "local"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"
    log_json: bool = False

    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "sherlock"
    db_user: str = "sherlock"
    db_password: str = "sherlock"

    database_url_raw: str | None = Field(default=None, alias="DATABASE_URL")
    database_url_sync_raw: str | None = Field(default=None, alias="DATABASE_URL_SYNC")

    client_api_key: str = "change-me-client"

    accounts_dir: Path = Path("./accounts")
    account_requests_per_hour: int = 30
    account_cooldown_seconds: int = 5
    account_response_timeout: int = 60
    account_resolve_username_per_day: int = 150

    sherlock_bot_username: str = "@SherlockBot"

    storage_dir: Path = Path("./storage")

    dispatcher_in_app: bool = False

    webhook_signing_secret: str = ""
    webhook_max_attempts: int = 8
    webhook_timeout_seconds: float = 10.0
    webhook_sweep_interval_seconds: float = 5.0
    webhook_backoff_base_seconds: float = 5.0
    webhook_backoff_max_seconds: float = 3600.0

    @property
    def normalized_client_api_key(self) -> str:
        return self.client_api_key.strip()

    @property
    def database_url(self) -> str:
        if self.database_url_raw:
            return self.database_url_raw
        return (
            "postgresql+asyncpg://"
            f"{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def database_url_sync(self) -> str:
        if self.database_url_sync_raw:
            return self.database_url_sync_raw
        return (
            "postgresql+psycopg://"
            f"{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
