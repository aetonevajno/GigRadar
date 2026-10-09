from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

from normalize import Event


class StorageError(Exception):
    pass


@dataclass(frozen=True)
class SyncOutcome:
    status: Literal["updated", "fresh", "busy"]
    event_count: int = 0
    rejected_count: int = 0


def _database_driver():
    try:
        import psycopg
        from psycopg.types.json import Jsonb
    except ImportError as exc:
        raise StorageError("Install requirements.txt to use PostgreSQL") from exc
    return psycopg, Jsonb


def _upsert_events(cursor, events: list[Event], jsonb) -> None:
    for event in events:
        cursor.execute(
            """
            INSERT INTO research_source_events
                (source, external_id, title, city, starts_at, venue, source_url,
                 artist_candidates, source_payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source, external_id) DO UPDATE SET
                title = EXCLUDED.title,
                city = EXCLUDED.city,
                starts_at = EXCLUDED.starts_at,
                venue = EXCLUDED.venue,
                source_url = EXCLUDED.source_url,
                artist_candidates = EXCLUDED.artist_candidates,
                source_payload = EXCLUDED.source_payload,
                last_seen_at = now()
            """,
            (
                event.source,
                event.external_id,
                event.title,
                event.city,
                event.starts_at,
                event.venue,
                event.url,
                jsonb(list(event.artist_candidates)),
                jsonb(event.payload),
            ),
        )


def store_events(database_url: str, events: list[Event]) -> int:
    psycopg, jsonb = _database_driver()
    schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    try:
        with psycopg.connect(database_url) as connection:
            connection.execute(schema)
            with connection.cursor() as cursor:
                _upsert_events(cursor, events, jsonb)
    except psycopg.Error as exc:
        raise StorageError(
            "PostgreSQL storage failed; inspect the database connection and schema"
        ) from exc
    return len(events)


def sync_timepad_city(
    database_url: str,
    city: str,
    since: date,
    until: date,
    refresh_after: timedelta,
    fetch_events: Callable[[], tuple[list[Event], int]],
    *,
    force: bool = False,
) -> SyncOutcome:
    psycopg, jsonb = _database_driver()
    schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    try:
        with psycopg.connect(database_url) as connection:
            connection.execute(schema)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtext(%s))",
                    (f"gigradar:timepad:{city}",),
                )
                if not cursor.fetchone()[0]:
                    return SyncOutcome("busy")
                cursor.execute(
                    """
                    SELECT window_start <= %s AND window_end >= %s
                           AND last_successful_at >= now() - %s
                    FROM research_sync_state
                    WHERE source = 'timepad' AND city = %s
                    """,
                    (since, until, refresh_after, city),
                )
                last_sync = cursor.fetchone()
                if not force and last_sync and last_sync[0]:
                    return SyncOutcome("fresh")
                events, rejected_count = fetch_events()
                event_ids = {(event.source, event.external_id) for event in events}
                if len(event_ids) != len(events) or any(
                    event.source != "timepad" or event.city != city for event in events
                ):
                    raise StorageError(
                        "Timepad sync returned duplicate IDs or wrong-city events"
                    )
                _upsert_events(cursor, events, jsonb)
                cursor.execute(
                    """
                    INSERT INTO research_sync_state
                        (source, city, window_start, window_end, last_successful_at,
                         event_count, rejected_count)
                    VALUES ('timepad', %s, %s, %s, now(), %s, %s)
                    ON CONFLICT (source, city) DO UPDATE SET
                        window_start = EXCLUDED.window_start,
                        window_end = EXCLUDED.window_end,
                        last_successful_at = EXCLUDED.last_successful_at,
                        event_count = EXCLUDED.event_count,
                        rejected_count = EXCLUDED.rejected_count
                    """,
                    (city, since, until, len(events), rejected_count),
                )
    except psycopg.Error as exc:
        raise StorageError("PostgreSQL sync failed") from exc
    return SyncOutcome("updated", len(events), rejected_count)


def list_catalog_events(
    database_url: str,
    since: datetime,
    until: datetime,
    city: str | None,
    limit: int,
    offset: int,
) -> list[dict[str, object]]:
    psycopg, _ = _database_driver()
    try:
        with (
            psycopg.connect(database_url) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                    SELECT source, external_id, title, city, starts_at, venue,
                           source_url, artist_candidates, last_seen_at
                    FROM research_source_events
                    WHERE starts_at >= %s AND starts_at < %s
                      AND (%s::text IS NULL OR city = %s)
                    ORDER BY starts_at, source, external_id
                    LIMIT %s OFFSET %s
                    """,
                (since, until, city, city, limit, offset),
            )
            rows = cursor.fetchall()
    except psycopg.Error as exc:
        raise StorageError("PostgreSQL catalog read failed") from exc
    return [
        {
            "source": source,
            "external_id": external_id,
            "title": title,
            "city": event_city,
            "starts_at": starts_at.isoformat(),
            "venue": venue,
            "source_url": source_url,
            "artist_candidates": artist_candidates,
            "last_seen_at": last_seen_at.isoformat(),
        }
        for (
            source,
            external_id,
            title,
            event_city,
            starts_at,
            venue,
            source_url,
            artist_candidates,
            last_seen_at,
        ) in rows
    ]
