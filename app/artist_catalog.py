from __future__ import annotations

import psycopg

from .user_actions import record_product_event


def find_artists(
    database: psycopg.Connection, query: str | None, *, limit: int, offset: int = 0
) -> dict:
    search = f"%{query.strip()}%" if query and query.strip() else None
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM artists a WHERE (a.musicbrainz_id IS NOT NULL OR EXISTS "
            "(SELECT 1 FROM concert_artists ca WHERE ca.artist_id = a.id)) "
            "AND (%s::text IS NULL OR a.name ILIKE %s OR EXISTS "
            "(SELECT 1 FROM artist_aliases aa WHERE aa.artist_id = a.id AND aa.alias ILIKE %s))",
            (search, search, search),
        )
        total = cursor.fetchone()[0]
        cursor.execute(
            "SELECT a.id, a.name FROM artists a WHERE (a.musicbrainz_id IS NOT NULL OR EXISTS "
            "(SELECT 1 FROM concert_artists ca WHERE ca.artist_id = a.id)) "
            "AND (%s::text IS NULL OR a.name ILIKE %s OR EXISTS "
            "(SELECT 1 FROM artist_aliases aa WHERE aa.artist_id = a.id AND aa.alias ILIKE %s)) "
            "ORDER BY a.name, a.id LIMIT %s OFFSET %s",
            (search, search, search, limit, offset),
        )
        rows = cursor.fetchall()
    if search is not None and offset == 0:
        record_product_event(
            database,
            None,
            "artist_search",
            "with_results" if total else "without_results",
        )
    return {
        "items": [{"id": artist_id, "name": name} for artist_id, name in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
