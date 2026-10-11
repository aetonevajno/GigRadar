from __future__ import annotations

import html
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import psycopg
from fastapi import HTTPException

from .user_actions import record_product_event

MOSCOW_TIME = ZoneInfo("Europe/Moscow")

CONCERT_COLUMNS = """
    c.id, c.title, c.city, c.starts_at, c.venue, c.status, c.updated_at,
    (SELECT json_agg(json_build_object('source', s.source, 'external_id', s.external_id,
        'url', s.source_url, 'last_seen_at', s.last_seen_at) ORDER BY s.source, s.external_id)
     FROM research_source_events s WHERE s.concert_id = c.id AND s.classification = 'concert') AS sources,
    (SELECT json_agg(json_build_object('id', a.id, 'name', a.name) ORDER BY a.name)
     FROM concert_artists ca JOIN artists a ON a.id = ca.artist_id
     WHERE ca.concert_id = c.id) AS artists
"""


def concert_row(row: tuple) -> dict:
    concert_id, title, city, starts_at, venue, status, updated_at, sources, artists = (
        row
    )
    return {
        "id": concert_id,
        "title": html.unescape(title),
        "city": city,
        "starts_at": starts_at.isoformat(),
        "venue": venue,
        "status": status,
        "updated_at": updated_at.isoformat(),
        "sources": sources or [],
        "artists": artists or [],
    }


def find_concerts(
    database: psycopg.Connection,
    *,
    city: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    artist_id: int | None = None,
    query: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    start = (
        datetime.combine(from_date, time.min, MOSCOW_TIME)
        if from_date
        else datetime.now(MOSCOW_TIME)
    )
    end = (
        datetime.combine(to_date + timedelta(days=1), time.min, MOSCOW_TIME)
        if to_date
        else start + timedelta(days=30)
    )
    if end <= start:
        raise HTTPException(422, "Date range is empty")
    filters = """c.starts_at >= %s AND c.starts_at < %s AND c.status <> 'cancelled'
        AND EXISTS (SELECT 1 FROM research_source_events s WHERE s.concert_id = c.id AND s.classification = 'concert')
        AND (%s::text IS NULL OR c.city = %s)
        AND (%s::bigint IS NULL OR EXISTS (SELECT 1 FROM concert_artists ca WHERE ca.concert_id = c.id AND ca.artist_id = %s))
        AND (%s::text IS NULL OR c.title ILIKE %s OR EXISTS (
            SELECT 1 FROM concert_artists ca JOIN artists a ON a.id = ca.artist_id
            WHERE ca.concert_id = c.id AND a.name ILIKE %s))"""
    search = f"%{query.strip()}%" if query and query.strip() else None
    parameters = (start, end, city, city, artist_id, artist_id, search, search, search)
    with database.cursor() as cursor:
        cursor.execute(f"SELECT count(*) FROM concerts c WHERE {filters}", parameters)
        total = cursor.fetchone()[0]
        cursor.execute(
            f"SELECT {CONCERT_COLUMNS} FROM concerts c WHERE {filters} "
            "ORDER BY c.starts_at, c.id LIMIT %s OFFSET %s",
            (*parameters, limit, offset),
        )
        rows = cursor.fetchall()
    if search is not None and offset == 0:
        record_product_event(
            database,
            None,
            "concert_search",
            "with_results" if total else "without_results",
        )
    return {
        "items": [concert_row(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
