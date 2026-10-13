import json
from functools import cache
from pathlib import Path

SUPPORTED_LOCALES = ("ru", "en")
_LOCALE_DIRECTORY = Path(__file__).resolve().parent


@cache
def translations_for(locale: str) -> dict:
    if locale not in SUPPORTED_LOCALES:
        raise ValueError(f"Unsupported locale: {locale}")
    return json.loads(
        (_LOCALE_DIRECTORY / f"{locale}.json").read_text(encoding="utf-8")
    )
