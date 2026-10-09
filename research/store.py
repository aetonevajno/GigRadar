from __future__ import annotations

from pathlib import Path

from normalize import Event


class StorageError(Exception):
    pass


def store_events(database_url: str, events: list[Event]) -> int:
    try:
        import psycopg
        from psycopg.types.json import Jsonb
    except ImportError as exc:
        raise StorageError("Install requirements.txt to use PostgreSQL") from exc

    schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    try:
        with psycopg.connect(database_url) as connection:
            connection.execute(schema)
            with connection.cursor() as cursor:
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
                            Jsonb(list(event.artist_candidates)),
                            Jsonb(event.payload),
                        ),
                    )
    except psycopg.Error as exc:
        raise StorageError(
            "PostgreSQL storage failed; inspect the database connection and schema"
        ) from exc
    return len(events)
