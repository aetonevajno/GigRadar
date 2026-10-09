from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def load_environment() -> None:
    load_dotenv(_ENV_FILE, override=False)


load_environment()


def database_url() -> str | None:
    return os.environ.get("DATABASE_URL")


def timepad_api_token() -> str | None:
    return os.environ.get("TIMEPAD_API_TOKEN")


def telegram_bot_token() -> str | None:
    return os.environ.get("TELEGRAM_BOT_TOKEN")
