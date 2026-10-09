from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

from curation import artist_mentions, assess_duplicate, classify, plain_text
from normalize import Event, normalize_kudago, normalize_timepad
from outbox import enqueue_change, enqueue_new_concert, reschedule_reminders


def apply_migrations(cursor) -> None:
    migrations = Path(__file__).with_name("migrations")
    for version, filename in (
        (1, "001_app.sql"),
        (2, "002_delivery.sql"),
        (3, "003_web_auth.sql"),
    ):
        cursor.execute("SELECT to_regclass('schema_migrations')")
        if cursor.fetchone()[0] is not None:
            cursor.execute(
                "SELECT 1 FROM schema_migrations WHERE version = %s", (version,)
            )
            if cursor.fetchone() is not None:
                continue
        cursor.execute((migrations / filename).read_text(encoding="utf-8"))


def _name_key(name: str) -> str:
    return re.sub(r"\s+", " ", plain_text(name).casefold()).strip()


def _artist_id(cursor, name: str) -> int:
    key = _name_key(name)
    cursor.execute("SELECT artist_id FROM artist_aliases WHERE alias_key = %s", (key,))
    alias = cursor.fetchone()
    if alias:
        return alias[0]
    cursor.execute(
        "INSERT INTO artists (name, name_key) VALUES (%s, %s) "
        "ON CONFLICT (name_key) DO UPDATE SET name = artists.name RETURNING id",
        (name, key),
    )
    return cursor.fetchone()[0]


def add_artist_alias(cursor, artist_id: int, alias: str) -> None:
    key = _name_key(alias)
    if not key:
        raise ValueError("Artist alias cannot be empty")
    cursor.execute("SELECT id FROM artists WHERE id = %s", (artist_id,))
    if cursor.fetchone() is None:
        raise ValueError("Artist does not exist")
    cursor.execute(
        "INSERT INTO artist_aliases (artist_id, alias, alias_key) VALUES (%s, %s, %s) "
        "ON CONFLICT (alias_key) DO UPDATE SET alias = EXCLUDED.alias "
        "WHERE artist_aliases.artist_id = EXCLUDED.artist_id",
        (artist_id, alias, key),
    )
    if cursor.rowcount == 0:
        raise ValueError("Alias already belongs to another artist")


def _source_status(event: Event) -> str:
    payload = event.payload
    status = payload.get("status")
    if payload.get("is_cancelled") is True or (
        isinstance(status, str) and status.casefold() in {"cancelled", "canceled"}
    ):
        return "cancelled"
    return "scheduled"


def _sync_artist_mentions(cursor, event: Event) -> set[str]:
    current_mentions = artist_mentions(event)
    active_mentions = {
        (mention.name, mention.field, mention.evidence) for mention in current_mentions
    }
    cursor.execute(
        "SELECT id, extracted_name, field, evidence FROM artist_mentions "
        "WHERE source = %s AND external_id = %s",
        (event.source, event.external_id),
    )
    for mention_id, name, field, evidence in cursor.fetchall():
        if (name, field, evidence) not in active_mentions:
            cursor.execute("DELETE FROM artist_mentions WHERE id = %s", (mention_id,))
    for mention in current_mentions:
        artist_id = (
            _artist_id(cursor, mention.name) if mention.confidence == "high" else None
        )
        cursor.execute(
            "INSERT INTO artist_mentions "
            "(source, external_id, artist_id, extracted_name, field, evidence, confidence) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (source, external_id, extracted_name, field, evidence) "
            "DO UPDATE SET confidence = EXCLUDED.confidence, "
            "artist_id = CASE WHEN artist_mentions.reviewed THEN artist_mentions.artist_id "
            "ELSE EXCLUDED.artist_id END",
            (
                event.source,
                event.external_id,
                artist_id,
                mention.name,
                mention.field,
                mention.evidence,
                mention.confidence,
            ),
        )
    cursor.execute(
        "SELECT DISTINCT a.name_key FROM artist_mentions m JOIN artists a ON a.id = m.artist_id "
        "WHERE m.source = %s AND m.external_id = %s",
        (event.source, event.external_id),
    )
    return {row[0] for row in cursor.fetchall()}


def _find_concert(cursor, event: Event, artist_keys: set[str]) -> tuple[int, bool]:
    cursor.execute(
        "SELECT source, external_id, title, city, starts_at, venue, "
        "source_url, artist_candidates, source_payload, concert_id "
        "FROM research_source_events WHERE classification = 'concert' "
        "AND city = %s AND starts_at BETWEEN %s AND %s "
        "AND concert_id IS NOT NULL AND NOT (source = %s AND external_id = %s) "
        "ORDER BY starts_at LIMIT 100",
        (
            event.city,
            event.starts_at - timedelta(hours=2),
            event.starts_at + timedelta(hours=2),
            event.source,
            event.external_id,
        ),
    )
    merge_ids: set[int] = set()
    merge_pairs: list[tuple[str, str, str]] = []
    reviews: list[tuple[str, str, str]] = []
    for (
        source,
        external_id,
        title,
        city,
        starts_at,
        venue,
        url,
        candidates,
        payload,
        candidate_concert,
    ) in cursor.fetchall():
        other = Event(
            source,
            external_id,
            title,
            city,
            starts_at,
            venue,
            url,
            tuple(candidates),
            payload,
        )
        cursor.execute(
            "SELECT a.name_key FROM artist_mentions m JOIN artists a ON a.id = m.artist_id "
            "WHERE m.source = %s AND m.external_id = %s",
            (source, external_id),
        )
        other_artists = {row[0] for row in cursor.fetchall()}
        assessment = assess_duplicate(event, other, artist_keys, other_artists)
        if assessment.decision == "merge":
            merge_ids.add(candidate_concert)
            merge_pairs.append((source, external_id, "multiple_exact_matches"))
        elif assessment.decision == "review":
            reviews.append((source, external_id, assessment.reason))
    if len(merge_ids) == 1:
        concert_id = merge_ids.pop()
        created = False
    else:
        cursor.execute(
            "INSERT INTO concerts (title, city, starts_at, venue) VALUES (%s, %s, %s, %s) RETURNING id",
            (event.title, event.city, event.starts_at, event.venue),
        )
        concert_id = cursor.fetchone()[0]
        created = True
        if merge_ids:
            reviews.extend(merge_pairs)
    for source, external_id, reason in reviews:
        first_key, second_key = sorted(
            ((event.source, event.external_id), (source, external_id))
        )
        cursor.execute(
            "INSERT INTO duplicate_reviews "
            "(first_source, first_external_id, second_source, second_external_id, reason) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (*first_key, *second_key, reason),
        )
    return concert_id, created


def _sync_concert_status(
    cursor, event: Event, old_row: tuple | None, concert_id: int, source_status: str
) -> None:
    if old_row is None and source_status == "cancelled":
        cursor.execute(
            "SELECT bool_and(source_status = 'cancelled') FROM research_source_events "
            "WHERE concert_id = %s AND classification = 'concert'",
            (concert_id,),
        )
        if cursor.fetchone()[0]:
            cursor.execute(
                "UPDATE concerts SET status = 'cancelled', updated_at = now() WHERE id = %s",
                (concert_id,),
            )
    if old_row and old_row[1] != event.starts_at and source_status == "scheduled":
        cursor.execute(
            "UPDATE concerts SET starts_at = %s, status = 'postponed', updated_at = now() WHERE id = %s",
            (event.starts_at, concert_id),
        )
        cursor.execute(
            "INSERT INTO concert_changes (concert_id, change_type, old_value, new_value, source, external_id) "
            "VALUES (%s, 'rescheduled', %s, %s, %s, %s) RETURNING id",
            (
                concert_id,
                old_row[1].isoformat() if old_row[1] else None,
                event.starts_at.isoformat(),
                event.source,
                event.external_id,
            ),
        )
        enqueue_change(cursor, concert_id, cursor.fetchone()[0], "rescheduled")
        reschedule_reminders(cursor, concert_id, event.starts_at)
    elif old_row and old_row[2] != source_status:
        cursor.execute(
            "SELECT bool_and(source_status = 'cancelled') FROM research_source_events "
            "WHERE concert_id = %s AND classification = 'concert'",
            (concert_id,),
        )
        cancelled = cursor.fetchone()[0]
        if cancelled or old_row[2] == "cancelled":
            new_status = "cancelled" if cancelled else "scheduled"
            change_type = "cancelled" if cancelled else "restored"
            cursor.execute(
                "UPDATE concerts SET status = %s, updated_at = now() WHERE id = %s",
                (new_status, concert_id),
            )
            cursor.execute(
                "INSERT INTO concert_changes (concert_id, change_type, old_value, new_value, source, external_id) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    concert_id,
                    change_type,
                    old_row[2],
                    new_status,
                    event.source,
                    event.external_id,
                ),
            )
            if cancelled:
                enqueue_change(cursor, concert_id, cursor.fetchone()[0], "cancelled")
                cursor.execute(
                    "DELETE FROM notification_outbox WHERE concert_id = %s AND kind = 'reminder' AND delivery_state = 'queued'",
                    (concert_id,),
                )


def curate_event(cursor, event: Event, old_row: tuple | None) -> None:
    classification = classify(event)
    source_status = _source_status(event)
    cursor.execute(
        "UPDATE research_source_events SET classification = %s, "
        "classification_reason = %s, source_status = %s "
        "WHERE source = %s AND external_id = %s",
        (
            classification.kind,
            classification.reason,
            source_status,
            event.source,
            event.external_id,
        ),
    )
    previous_concert = old_row[0] if old_row else None
    if classification.kind != "concert":
        if previous_concert:
            _refresh_concert_artists(cursor, previous_concert)
        return

    artist_keys = _sync_artist_mentions(cursor, event)

    if previous_concert:
        concert_id, created = previous_concert, False
    else:
        concert_id, created = _find_concert(cursor, event, artist_keys)

    cursor.execute(
        "UPDATE research_source_events SET concert_id = %s WHERE source = %s AND external_id = %s",
        (concert_id, event.source, event.external_id),
    )
    _refresh_concert_artists(cursor, concert_id)
    cursor.execute(
        "SELECT count(*) FROM research_source_events WHERE concert_id = %s",
        (concert_id,),
    )
    if cursor.fetchone()[0] == 1:
        cursor.execute(
            "UPDATE concerts SET title = %s, city = %s, venue = %s, updated_at = now() "
            "WHERE id = %s AND (title, city, venue) IS DISTINCT FROM (%s, %s, %s)",
            (
                event.title,
                event.city,
                event.venue,
                concert_id,
                event.title,
                event.city,
                event.venue,
            ),
        )
    _sync_concert_status(cursor, event, old_row, concert_id, source_status)
    if created:
        enqueue_new_concert(cursor, concert_id)


def _refresh_concert_artists(cursor, concert_id: int) -> None:
    cursor.execute("DELETE FROM concert_artists WHERE concert_id = %s", (concert_id,))
    cursor.execute(
        "INSERT INTO concert_artists (concert_id, artist_id) "
        "SELECT DISTINCT %s, m.artist_id FROM artist_mentions m "
        "JOIN research_source_events s ON (s.source, s.external_id) = (m.source, m.external_id) "
        "WHERE s.concert_id = %s AND s.classification = 'concert' AND m.artist_id IS NOT NULL "
        "ON CONFLICT DO NOTHING",
        (concert_id, concert_id),
    )


def review_artist_mention(cursor, mention_id: int, artist_id: int | None) -> None:
    if artist_id is not None:
        cursor.execute("SELECT id FROM artists WHERE id = %s", (artist_id,))
        if cursor.fetchone() is None:
            raise ValueError("Artist does not exist")
    cursor.execute(
        "UPDATE artist_mentions SET artist_id = %s, reviewed = true WHERE id = %s RETURNING source, external_id",
        (artist_id, mention_id),
    )
    source_record = cursor.fetchone()
    if source_record is None:
        raise ValueError("Artist mention does not exist")
    cursor.execute(
        "SELECT concert_id FROM research_source_events WHERE source = %s AND external_id = %s",
        source_record,
    )
    concert_id = cursor.fetchone()[0]
    if concert_id:
        _refresh_concert_artists(cursor, concert_id)


def backfill(database_url: str) -> int:
    import psycopg

    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        apply_migrations(cursor)
        cursor.execute(
            "SELECT source_payload FROM research_source_events ORDER BY source, external_id"
        )
        payloads = cursor.fetchall()
        for (payload,) in payloads:
            event = (
                normalize_timepad(payload)
                if payload.get("starts_at")
                else normalize_kudago(payload)
            )
            cursor.execute(
                "SELECT concert_id, starts_at, source_status FROM research_source_events "
                "WHERE source = %s AND external_id = %s",
                (event.source, event.external_id),
            )
            existing = cursor.fetchone()
            cursor.execute(
                "UPDATE research_source_events SET title = %s "
                "WHERE source = %s AND external_id = %s AND title IS DISTINCT FROM %s",
                (event.title, event.source, event.external_id, event.title),
            )
            curate_event(cursor, event, existing)
    return len(payloads)
