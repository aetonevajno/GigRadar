from __future__ import annotations

import psycopg


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
