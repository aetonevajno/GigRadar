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
    mark_user_active(database, row[0])
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


def mark_user_active(database: psycopg.Connection, user_id: int) -> None:
    database.execute(
        "INSERT INTO user_activity (activity_date, user_id) VALUES (current_date, %s) ON CONFLICT DO NOTHING",
        (user_id,),
    )
