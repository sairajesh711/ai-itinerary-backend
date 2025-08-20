# config.py
from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass
from dotenv import load_dotenv

# Load .env from project root (if present). Safe to call multiple times.
load_dotenv(dotenv_path=Path(".") / ".env", override=False)


@dataclass(frozen=True)
class Settings:
    # Core app
    APP_ENV: str = os.getenv("APP_ENV", "development")
    DEBUG: bool = os.getenv("DEBUG", "false").lower() in {"1", "true", "yes"}

    # OpenAI
    OPENAI_API_KEY: str | None = os.getenv("OPENAI_API_KEY")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # placeholder; tweak later

    # Defaults for Phase 1 (can evolve)
    DEFAULT_CURRENCY: str = os.getenv("DEFAULT_CURRENCY", "EUR")


settings = Settings()
