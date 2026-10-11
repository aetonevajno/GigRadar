from __future__ import annotations

import psycopg


def subscribe_artist(
    database: psycopg.Connection, user_id: int, artist_id: int
) -> bool:
    if (
        database.execute("SELECT 1 FROM artists WHERE id = %s", (artist_id,)).fetchone()
        is None
    ):
        return False
    database.execute(
        "INSERT INTO user_artist_subscriptions (user_id, artist_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (user_id, artist_id),
    )
    return True


def unsubscribe_artist(
    database: psycopg.Connection, user_id: int, artist_id: int
) -> None:
    database.execute(
        "DELETE FROM user_artist_subscriptions WHERE user_id = %s AND artist_id = %s",
        (user_id, artist_id),
    )


def add_favorite(database: psycopg.Connection, user_id: int, concert_id: int) -> bool:
    with database.transaction():
        row = database.execute(
            "SELECT starts_at FROM concerts c WHERE c.id = %s AND c.status <> 'cancelled' "
            "AND EXISTS (SELECT 1 FROM research_source_events s "
            "WHERE s.concert_id = c.id AND s.classification = 'concert')",
            (concert_id,),
        ).fetchone()
        if row is None:
            return False
        database.execute(
            "INSERT INTO user_favorites (user_id, concert_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (user_id, concert_id),
        )
        database.execute(
            "INSERT INTO notification_outbox (user_id, concert_id, kind, dedupe_key, due_at) "
            "SELECT %s, %s, 'reminder', %s, GREATEST(now(), %s::timestamptz - interval '1 day') "
            "WHERE %s::timestamptz > now() ON CONFLICT (dedupe_key) DO NOTHING",
            (
                user_id,
                concert_id,
                f"reminder:{user_id}:{concert_id}:{row[0].isoformat()}",
                row[0],
                row[0],
            ),
        )
    return True


def remove_favorite(
    database: psycopg.Connection, user_id: int, concert_id: int
) -> None:
    with database.transaction():
        database.execute(
            "DELETE FROM user_favorites WHERE user_id = %s AND concert_id = %s",
            (user_id, concert_id),
        )
        database.execute(
            "DELETE FROM notification_outbox WHERE user_id = %s AND concert_id = %s "
            "AND kind = 'reminder' AND sent_at IS NULL",
            (user_id, concert_id),
        )
