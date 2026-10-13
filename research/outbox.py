from __future__ import annotations

from datetime import UTC


def enqueue_new_concert(
    cursor, concert_id: int, artist_ids: list[int] | None = None
) -> None:
    cursor.execute(
        "INSERT INTO notification_outbox (user_id, concert_id, kind, dedupe_key, due_at) "
        "SELECT DISTINCT u.id, c.id, 'new_concert', 'new:' || u.id || ':' || c.id, now() "
        "FROM concerts c JOIN concert_artists ca ON ca.concert_id = c.id "
        "JOIN user_artist_subscriptions s ON s.artist_id = ca.artist_id "
        "JOIN users u ON u.id = s.user_id "
        "WHERE c.id = %s AND (%s::bigint[] IS NULL OR ca.artist_id = ANY(%s::bigint[])) "
        "AND c.status = 'scheduled' AND c.starts_at > now() "
        "AND u.notifications_enabled AND u.bot_chat_available "
        "AND (u.city IS NULL OR u.city = c.city) "
        "ON CONFLICT (dedupe_key) DO NOTHING",
        (concert_id, artist_ids, artist_ids),
    )


def enqueue_change(cursor, concert_id: int, change_id: int, kind: str) -> None:
    cursor.execute(
        "INSERT INTO notification_outbox "
        "(user_id, concert_id, kind, change_id, dedupe_key, due_at) "
        "SELECT DISTINCT u.id, c.id, %s, %s, %s || ':' || u.id || ':' || %s, now() "
        "FROM concerts c JOIN users u ON u.notifications_enabled AND u.bot_chat_available "
        "WHERE c.id = %s AND (EXISTS "
        "(SELECT 1 FROM user_favorites f WHERE f.user_id = u.id AND f.concert_id = c.id) "
        "OR EXISTS (SELECT 1 FROM concert_artists ca JOIN user_artist_subscriptions s "
        "ON s.artist_id = ca.artist_id WHERE ca.concert_id = c.id AND s.user_id = u.id)) "
        "AND (%s <> 'restored' OR EXISTS (SELECT 1 FROM notification_outbox prior "
        "WHERE prior.user_id = u.id AND prior.concert_id = c.id AND prior.kind = 'cancelled' "
        "AND prior.delivery_state IN ('sent', 'uncertain'))) "
        "ON CONFLICT (dedupe_key) DO NOTHING",
        (kind, change_id, kind, change_id, concert_id, kind),
    )


def reschedule_reminders(cursor, concert_id: int, starts_at) -> None:
    cursor.execute(
        "DELETE FROM notification_outbox WHERE concert_id = %s AND kind = 'reminder' "
        "AND delivery_state = 'queued'",
        (concert_id,),
    )
    cursor.execute(
        "INSERT INTO notification_outbox (user_id, concert_id, kind, dedupe_key, due_at) "
        "SELECT f.user_id, f.concert_id, 'reminder', "
        "'reminder:' || f.user_id || ':' || f.concert_id || ':' || %s, "
        "GREATEST(now(), %s::timestamptz - interval '1 day') "
        "FROM user_favorites f WHERE f.concert_id = %s AND %s::timestamptz > now() "
        "ON CONFLICT (dedupe_key) DO UPDATE SET delivery_state = 'queued', "
        "failure_reason = NULL, last_error = NULL, attempts = 0, "
        "due_at = EXCLUDED.due_at WHERE notification_outbox.delivery_state = 'failed' "
        "AND notification_outbox.failure_reason = 'ineligible'",
        (starts_at.astimezone(UTC).isoformat(), starts_at, concert_id, starts_at),
    )
