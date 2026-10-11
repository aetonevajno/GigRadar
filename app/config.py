from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def load_environment() -> None:
    load_dotenv(_ENV_FILE, override=False)


load_environment()


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is required")
    return url


def telegram_bot_token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")


def telegram_app_url() -> str | None:
    url = os.environ.get("TELEGRAM_APP_URL", "").strip()
    if not url:
        return None
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("TELEGRAM_APP_URL must be an HTTPS URL")
    return url


def oidc_client_id() -> str:
    return os.environ.get("TELEGRAM_OIDC_CLIENT_ID", "")


def oidc_client_secret() -> str:
    return os.environ.get("TELEGRAM_OIDC_CLIENT_SECRET", "")


def oidc_redirect_uri() -> str:
    return os.environ.get("TELEGRAM_OIDC_REDIRECT_URI", "")


def cors_origins() -> str:
    return os.environ.get("CORS_ORIGINS", "http://localhost:5173")
