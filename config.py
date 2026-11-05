"""Centralized configuration and security policy settings."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from dotenv import load_dotenv

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
    # If launched directly from dist/FormDScoutBot.exe, also check parent workspace for .env
    if not (BASE_DIR / ".env").exists() and (BASE_DIR.parent / ".env").exists():
        BASE_DIR = BASE_DIR.parent
else:
    BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")


def _parse_discord_ids(raw_val: str) -> set[int]:
    """Extracts numeric Discord IDs from a comma, semicolon, or whitespace separated string."""
    ids: set[int] = set()
    for token in re.split(r"[,;\s]+", raw_val.strip()):
        cleaned = re.sub(r"\D", "", token)
        if cleaned:
            ids.add(int(cleaned))
    return ids


class Settings:
    """Immutable runtime settings loaded from environment variables."""

    # Discord & Multi-User Security
    _raw_owners: str = os.getenv("OWNER_DISCORD_ID", "") or os.getenv("AUTHORIZED_USER_IDS", "")
    AUTHORIZED_USER_IDS: set[int] = _parse_discord_ids(_raw_owners)
    PRIMARY_OWNER_ID: int = next(iter(AUTHORIZED_USER_IDS), 0)
    OWNER_DISCORD_ID: int = PRIMARY_OWNER_ID  # Backwards compatibility alias
    DISCORD_BOT_TOKEN: str = os.getenv("DISCORD_BOT_TOKEN", "").strip()

    # Filing Sources & Loop Timing
    SEC_EDGAR_ATOM_URL: str = (
        "https://www.sec.gov/cgi-bin/browse-edgar"
        "?action=getcurrent&type=D&count=100&output=atom"
    )
    SEC_USER_AGENT: str = os.getenv(
        "SEC_USER_AGENT", "FormDScoutBot contact@example.com"
    ).strip()
    DEFAULT_POLL_INTERVAL_MINUTES: float = float(
        os.getenv("DEFAULT_POLL_INTERVAL_MINUTES", "15")
    )
    SEDAR_ENABLED: bool = os.getenv("SEDAR_ENABLED", "true").lower() in ("1", "true", "yes")
    SEDAR_MIN_COOLDOWN_MINUTES: float = float(
        os.getenv("SEDAR_MIN_COOLDOWN_MINUTES", "15")
    )
    CAD_TO_USD_RATE: float = float(os.getenv("CAD_TO_USD_RATE", "0.72"))

    # Default Baseline Criteria
    DEFAULT_MIN_RAISE_USD: float = 8000.0
    EXCLUDE_POOLED_FUNDS_BY_DEFAULT: bool = True

    # OpenRouter
    OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "").strip()
    OPENROUTER_MODEL: str = os.getenv(
        "OPENROUTER_MODEL", "google/gemini-2.5-flash"
    ).strip()
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1/chat/completions"

    # Exa.ai
    EXA_API_KEY: str = os.getenv("EXA_API_KEY", "").strip()
    EXA_SEARCH_URL: str = "https://api.exa.ai/search"
    EXA_COST_PER_SEARCH_USD: float = 0.005  # Standard Exa neural search estimate

    # TypeSafe.ai (System One / Jev)
    TYPESAFE_API_KEY: str = os.getenv("TYPESAFE_API_KEY", "").strip()
    TYPESAFE_MODEL: str = os.getenv("TYPESAFE_MODEL", "jev-latest").strip()
    TYPESAFE_URL: str = "https://api.typesafe.ai/v1/systemone"
    TYPESAFE_CONFIDENCE_THRESHOLD: float = float(
        os.getenv("TYPESAFE_CONFIDENCE_THRESHOLD", "0.70")
    )
    TYPESAFE_INPUT_TOKEN_USD: float = 0.0000005
    TYPESAFE_OUTPUT_TOKEN_USD: float = 0.0000015

    # Contact Enrichment (Apify & Apollo.io)
    ENRICHER_PROVIDER: str = os.getenv("ENRICHER_PROVIDER", "apify").strip().lower()
    APIFY_API_TOKEN: str = os.getenv("APIFY_API_TOKEN", "").strip()
    APIFY_LINKEDIN_EMAIL_ACTOR: str = os.getenv(
        "APIFY_LINKEDIN_EMAIL_ACTOR", "vulnv/linkedin-email-finder"
    ).strip()
    APIFY_EMAIL_VERIFIER_ACTOR: str = os.getenv(
        "APIFY_EMAIL_VERIFIER_ACTOR", "michael.g/email-verifier-validator"
    ).strip()
    APOLLO_API_KEY: str = os.getenv("APOLLO_API_KEY", "").strip()

    # Local SQLite path (excluded from Git via .gitignore)
    DB_PATH: Path = BASE_DIR / "bot_state.db"


settings = Settings()
