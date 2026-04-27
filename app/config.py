"""Centralized application configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed settings backed by environment variables / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # App
    app_name: str = Field(default="Jurinex_call_agent")
    app_env: Literal["development", "staging", "production"] = "development"
    debug: bool = True
    log_level: str = "DEBUG"
    demo_mode: bool = True

    public_base_url: str = "http://localhost:8000"

    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""

    # Gemini
    gemini_api_key: str = ""
    google_api_key: str = ""
    gemini_model: str = "gemini-3.1-flash-live-preview"
    gemini_voice: str = "Aoede"

    # Database
    database_url: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/jurinex_call_agent"
    )
    sync_database_url: str = (
        "postgresql+psycopg2://postgres:postgres@localhost:5432/jurinex_call_agent"
    )

    # Cloud SQL
    cloud_sql_connection_name: str = ""
    db_user: str = "postgres"
    db_password: str = "postgres"
    db_name: str = "jurinex_call_agent"

    # Auth
    secret_key: str = "change_this_secret"
    admin_api_key: str = "change_me"

    @property
    def gemini_key(self) -> str:
        """Return whichever Gemini key is configured."""
        return self.gemini_api_key or self.google_api_key

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor."""
    return Settings()


settings = get_settings()
