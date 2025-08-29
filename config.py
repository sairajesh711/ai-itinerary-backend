# config.py
from __future__ import annotations
from typing import List, Optional, Literal
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, AliasChoices, model_validator

class Settings(BaseSettings):
    # Read .env; ignore extra env vars to avoid crashes
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        env_nested_delimiter="__",
        env_prefix="",  # no automatic prefix
    )

    # --- Runtime env / debugging ---
    APP_ENV: Literal["development", "staging", "production"] = Field(
        default="development",
        validation_alias=AliasChoices("APP_ENV", "app_env"),
    )
    DEBUG: bool = Field(
        default=False,
        validation_alias=AliasChoices("DEBUG", "debug"),
    )

    # --- OpenAI / app defaults ---
    OPENAI_API_KEY: str = Field(
        default="",
        validation_alias=AliasChoices("OPENAI_API_KEY", "openai_api_key"),
    )
    OPENAI_MODEL: str = Field(
        default="gpt-4o",
        validation_alias=AliasChoices("OPENAI_MODEL", "openai_model"),
    )
    DEFAULT_CURRENCY: str = Field(
        default="USD",
        validation_alias=AliasChoices("DEFAULT_CURRENCY", "default_currency"),
    )
    
    # --- Server Settings (for deployment) ---
    PORT: int = Field(
        default=8000,
        validation_alias=AliasChoices("PORT", "port"),
    )
    HOST: str = Field(
        default="0.0.0.0",
        validation_alias=AliasChoices("HOST", "host"),
    )
    
    # --- Logging ---
    LOG_LEVEL: str = Field(
        default="INFO",
        validation_alias=AliasChoices("LOG_LEVEL", "log_level"),
    )

    # --- CORS (env-driven) ---
    CORS_ALLOW_ORIGINS: List[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173", "http://127.0.0.1:5174", "http://localhost:5174"],
        validation_alias=AliasChoices("CORS_ALLOW_ORIGINS", "cors_allow_origins"),
    )
    # Optional comma-separated alternative that overrides the above
    FRONTEND_ORIGINS: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("FRONTEND_ORIGINS", "frontend_origins"),
    )

    CORS_ALLOW_CREDENTIALS: bool = Field(
        default=False,
        validation_alias=AliasChoices("CORS_ALLOW_CREDENTIALS", "cors_allow_credentials"),
    )
    CORS_ALLOW_METHODS: List[str] = Field(
        default_factory=lambda: ["GET", "POST", "OPTIONS"],
        validation_alias=AliasChoices("CORS_ALLOW_METHODS", "cors_allow_methods"),
    )
    CORS_ALLOW_HEADERS: List[str] = Field(
        default_factory=lambda: [
            "Accept", 
            "Accept-Language", 
            "Authorization", 
            "Content-Language", 
            "Content-Type", 
            "X-Request-Id",
            "Cache-Control",
            "Pragma",
            "Expires"
        ],
        validation_alias=AliasChoices("CORS_ALLOW_HEADERS", "cors_allow_headers"),
    )
    CORS_EXPOSE_HEADERS: List[str] = Field(
        default_factory=lambda: ["X-Request-Id"],
        validation_alias=AliasChoices("CORS_EXPOSE_HEADERS", "cors_expose_headers"),
    )
    CORS_MAX_AGE: int = Field(
        default=86400,
        validation_alias=AliasChoices("CORS_MAX_AGE", "cors_max_age"),
    )

    @model_validator(mode="after")
    def _merge_frontend_origins(self) -> "Settings":
        if self.FRONTEND_ORIGINS:
            parts = [p.strip() for p in self.FRONTEND_ORIGINS.split(",") if p.strip()]
            if parts:
                self.CORS_ALLOW_ORIGINS = parts
        return self
    
    @model_validator(mode="after")
    def _validate_production_settings(self) -> "Settings":
        """Validate critical settings for production deployment."""
        if self.APP_ENV == "production":
            # Check for placeholder API key
            if not self.OPENAI_API_KEY or self.OPENAI_API_KEY in [
                "", 
                "your-openai-api-key-here",
                "sk-proj-YOUR_ACTUAL_OPENAI_API_KEY_HERE"
            ]:
                raise ValueError(
                    "OPENAI_API_KEY must be set to a valid key in production. "
                    "Get your key from https://platform.openai.com/api-keys"
                )
            
            # Ensure production CORS origins don't include localhost
            localhost_origins = [origin for origin in self.CORS_ALLOW_ORIGINS 
                               if "localhost" in origin or "127.0.0.1" in origin]
            if localhost_origins:
                import logging
                logging.getLogger("config").warning(
                    f"Production environment includes localhost CORS origins: {localhost_origins}. "
                    "Consider removing these for production deployment."
                )
        
        return self

    @property
    def is_dev(self) -> bool:
        return self.APP_ENV == "development"

    @property
    def log_level(self) -> str:
        return "DEBUG" if self.DEBUG else "INFO"

settings = Settings()
