from __future__ import annotations

from pydantic import Field, AliasChoices
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    """
    Centralized app settings loaded from environment / .env.
    - We accept both UPPER and lower-case env var names via AliasChoices.
    - We ignore unknown .env keys so you can keep extras without breaking boot.
    """
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # <-- critical: don't crash on unknown keys like 'app_env', 'debug'
    )

    # Core Phase-1 settings
    OPENAI_API_KEY: str = Field(
        default="",
        validation_alias=AliasChoices("OPENAI_API_KEY", "openai_api_key"),
    )
    OPENAI_MODEL: str = Field(
        default="gpt-4o-mini",
        validation_alias=AliasChoices("OPENAI_MODEL", "openai_model"),
    )
    DEFAULT_CURRENCY: str = Field(
        default="EUR",
        validation_alias=AliasChoices("DEFAULT_CURRENCY", "default_currency"),
    )

    # Optional convenience flags (since your .env contains them)
    app_env: str = Field(
        default="development",
        validation_alias=AliasChoices("APP_ENV", "app_env"),
        description="Arbitrary environment name (development|staging|production)."
    )
    debug: bool = Field(
        default=False,
        validation_alias=AliasChoices("DEBUG", "debug"),
        description="Enable extra logging / verbose traces."
    )

settings = Settings()
