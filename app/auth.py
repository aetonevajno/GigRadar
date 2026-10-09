from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl


class InvalidInitData(ValueError):
    pass


@dataclass(frozen=True)
class TelegramIdentity:
    telegram_id: int
    display_name: str
    username: str | None


def verify_init_data(
    raw: str, bot_token: str, *, now: int | None = None, max_age_seconds: int = 86400
) -> TelegramIdentity:
    if not raw or len(raw) > 8192 or not bot_token:
        raise InvalidInitData("Missing Telegram authentication data")
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise InvalidInitData("Malformed Telegram authentication data") from exc
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise InvalidInitData("Duplicate Telegram authentication field")
    supplied_hash = fields.pop("hash", "")
    if len(supplied_hash) != 64:
        raise InvalidInitData("Missing Telegram signature")
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, supplied_hash):
        raise InvalidInitData("Invalid Telegram signature")
    try:
        authenticated_at = int(fields["auth_date"])
        user = json.loads(fields["user"])
        telegram_id = user["id"]
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise InvalidInitData("Invalid Telegram user data") from exc
    current_time = int(time.time()) if now is None else now
    if (
        authenticated_at > current_time + 60
        or current_time - authenticated_at > max_age_seconds
    ):
        raise InvalidInitData("Telegram authentication has expired")
    if not isinstance(user, dict) or type(telegram_id) is not int or telegram_id <= 0:
        raise InvalidInitData("Invalid Telegram user ID")
    first_name = user.get("first_name")
    last_name = user.get("last_name")
    username = user.get("username")
    if not isinstance(first_name, str) or not first_name.strip():
        raise InvalidInitData("Invalid Telegram display name")
    display_name = " ".join(
        part
        for part in (
            first_name.strip(),
            last_name.strip() if isinstance(last_name, str) else "",
        )
        if part
    )
    return TelegramIdentity(
        telegram_id,
        display_name[:200],
        username[:100] if isinstance(username, str) else None,
    )
