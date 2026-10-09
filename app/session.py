from __future__ import annotations

import hmac
import os
from typing import Annotated

import psycopg
from fastapi import Depends, Header, HTTPException, Request

from .auth import InvalidInitData, verify_init_data
from .web_auth import csrf_token, token_hash, valid_token

WEB_SESSION_COOKIE = "gigradar_session"


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is required")
    return url


def connection():
    try:
        with psycopg.connect(database_url()) as database:
            yield database
    except psycopg.OperationalError as exc:
        raise HTTPException(503, "Database is temporarily unavailable") from exc


Database = Annotated[psycopg.Connection, Depends(connection)]


def web_session_identity(
    request: Request, database: psycopg.Connection
) -> tuple[int, str]:
    session_token = request.cookies.get(WEB_SESSION_COOKIE, "")
    if not valid_token(session_token):
        raise HTTPException(401, "Web session is missing or invalid")
    row = database.execute(
        "SELECT user_id FROM web_sessions WHERE token_hash = %s AND expires_at > now()",
        (token_hash(session_token),),
    ).fetchone()
    if row is None:
        raise HTTPException(401, "Web session has expired")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        submitted = request.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(submitted, csrf_token(session_token)):
            raise HTTPException(403, "Invalid CSRF token")
    return row[0], session_token


def profile_id(
    database: Database,
    request: Request,
    x_telegram_init_data: Annotated[str | None, Header()] = None,
) -> int:
    if x_telegram_init_data:
        try:
            telegram = verify_init_data(
                x_telegram_init_data, os.environ.get("TELEGRAM_BOT_TOKEN", "")
            )
        except InvalidInitData as exc:
            raise HTTPException(401, str(exc)) from exc
        row = database.execute(
            "SELECT id FROM users WHERE telegram_id = %s", (telegram.telegram_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(401, "Complete Telegram sign in first")
        return row[0]
    return web_session_identity(request, database)[0]


ProfileId = Annotated[int, Depends(profile_id)]


def upsert_user(
    database: psycopg.Connection,
    telegram_id: int,
    display_name: str,
    username: str | None,
) -> dict:
    row = database.execute(
        "INSERT INTO users (telegram_id, display_name, username) VALUES (%s, %s, %s) "
        "ON CONFLICT (telegram_id) DO UPDATE SET display_name = EXCLUDED.display_name, "
        "username = EXCLUDED.username RETURNING id, telegram_id, display_name, username, city, notifications_enabled",
        (telegram_id, display_name, username),
    ).fetchone()
    return dict(
        zip(
            (
                "id",
                "telegram_id",
                "display_name",
                "username",
                "city",
                "notifications_enabled",
            ),
            row,
        )
    )


def get_profile(database: psycopg.Connection, user_id: int) -> dict:
    row = database.execute(
        "SELECT id, telegram_id, display_name, username, city, notifications_enabled FROM users WHERE id = %s",
        (user_id,),
    ).fetchone()
    return dict(
        zip(
            (
                "id",
                "telegram_id",
                "display_name",
                "username",
                "city",
                "notifications_enabled",
            ),
            row,
        )
    )
