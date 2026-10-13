from __future__ import annotations

import psycopg


def upsert_user(
    database: psycopg.Connection,
    telegram_id: int,
    display_name: str,
    username: str | None,
    *,
    private_bot_chat: bool = False,
) -> dict:
    row = database.execute(
        "INSERT INTO users (telegram_id, display_name, username, notifications_enabled, bot_chat_available) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (telegram_id) DO UPDATE SET display_name = EXCLUDED.display_name, "
        "username = EXCLUDED.username, "
        "bot_chat_available = users.bot_chat_available OR EXCLUDED.bot_chat_available "
        "RETURNING id, telegram_id, display_name, username, city, "
        "notifications_enabled AND bot_chat_available, locale, bot_chat_available",
        (telegram_id, display_name, username, private_bot_chat, private_bot_chat),
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
                "locale",
                "bot_chat_available",
            ),
            row,
        )
    )


def mark_user_active(database: psycopg.Connection, user_id: int) -> None:
    database.execute(
        "INSERT INTO user_activity (activity_date, user_id) VALUES (current_date, %s) ON CONFLICT DO NOTHING",
        (user_id,),
    )
