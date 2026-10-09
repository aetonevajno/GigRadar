from __future__ import annotations

import html
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import psycopg
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .auth import InvalidInitData, verify_init_data
from .config import cors_origins, telegram_bot_token
from .session import Database, ProfileId, database_url, get_profile, upsert_user
from .web_routes import router as web_auth_router

MOSCOW_TIME = ZoneInfo("Europe/Moscow")
RESEARCH_DIR = Path(__file__).resolve().parents[1] / "research"


@asynccontextmanager
async def lifespan(_: FastAPI):
    with psycopg.connect(database_url()) as connection:
        connection.execute((RESEARCH_DIR / "schema.sql").read_text(encoding="utf-8"))
        for version, filename in (
            (1, "001_app.sql"),
            (2, "002_delivery.sql"),
            (3, "003_web_auth.sql"),
        ):
            if (
                connection.execute(
                    "SELECT to_regclass('schema_migrations')"
                ).fetchone()[0]
                is not None
                and connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = %s", (version,)
                ).fetchone()
            ):
                continue
            connection.execute(
                (RESEARCH_DIR / "migrations" / filename).read_text(encoding="utf-8")
            )
    yield


app = FastAPI(title="GigRadar", lifespan=lifespan)
app.include_router(web_auth_router)
origins = [origin.strip() for origin in cors_origins().split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    allow_headers=["X-Telegram-Init-Data", "X-CSRF-Token", "Content-Type"],
)


@app.exception_handler(psycopg.Error)
async def database_error(_, __: psycopg.Error):
    return JSONResponse(
        status_code=503, content={"detail": "Database is temporarily unavailable"}
    )


class TelegramLogin(BaseModel):
    init_data: str


class ProfileUpdate(BaseModel):
    city: str | None = None
    notifications_enabled: bool | None = None


def _concert_row(row: tuple) -> dict:
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


_CONCERT_COLUMNS = """
    c.id, c.title, c.city, c.starts_at, c.venue, c.status, c.updated_at,
    (SELECT json_agg(json_build_object('source', s.source, 'external_id', s.external_id,
        'url', s.source_url, 'last_seen_at', s.last_seen_at) ORDER BY s.source, s.external_id)
     FROM research_source_events s WHERE s.concert_id = c.id AND s.classification = 'concert') AS sources,
    (SELECT json_agg(json_build_object('id', a.id, 'name', a.name) ORDER BY a.name)
     FROM concert_artists ca JOIN artists a ON a.id = ca.artist_id
     WHERE ca.concert_id = c.id) AS artists
"""


@app.get("/health")
def health(database: Database):
    database.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/concerts")
def concerts(
    database: Database,
    city: str | None = None,
    from_date: Annotated[date | None, Query(alias="from")] = None,
    to_date: Annotated[date | None, Query(alias="to")] = None,
    artist_id: int | None = None,
    q: str | None = Query(None, max_length=100),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
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
    search = f"%{q.strip()}%" if q and q.strip() else None
    parameters = (start, end, city, city, artist_id, artist_id, search, search, search)
    with database.cursor() as cursor:
        cursor.execute(f"SELECT count(*) FROM concerts c WHERE {filters}", parameters)
        total = cursor.fetchone()[0]
        cursor.execute(
            f"SELECT {_CONCERT_COLUMNS} FROM concerts c WHERE {filters} "
            "ORDER BY c.starts_at, c.id LIMIT %s OFFSET %s",
            (*parameters, limit, offset),
        )
        rows = cursor.fetchall()
    return {
        "items": [_concert_row(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.get("/concerts/{concert_id}")
def concert(concert_id: int, database: Database):
    with database.cursor() as cursor:
        cursor.execute(
            f"SELECT {_CONCERT_COLUMNS} FROM concerts c WHERE c.id = %s AND EXISTS "
            "(SELECT 1 FROM research_source_events s WHERE s.concert_id = c.id AND s.classification = 'concert')",
            (concert_id,),
        )
        row = cursor.fetchone()
    if row is None:
        raise HTTPException(404, "Concert not found")
    return _concert_row(row)


@app.get("/artists")
def artists(
    database: Database,
    q: str | None = Query(None, max_length=100),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    search = f"%{q.strip()}%" if q and q.strip() else None
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM artists a WHERE EXISTS (SELECT 1 FROM concert_artists ca WHERE ca.artist_id = a.id) "
            "AND (%s::text IS NULL OR a.name ILIKE %s OR EXISTS "
            "(SELECT 1 FROM artist_aliases aa WHERE aa.artist_id = a.id AND aa.alias ILIKE %s))",
            (search, search, search),
        )
        total = cursor.fetchone()[0]
        cursor.execute(
            "SELECT a.id, a.name FROM artists a WHERE EXISTS (SELECT 1 FROM concert_artists ca WHERE ca.artist_id = a.id) "
            "AND (%s::text IS NULL OR a.name ILIKE %s OR EXISTS "
            "(SELECT 1 FROM artist_aliases aa WHERE aa.artist_id = a.id AND aa.alias ILIKE %s)) "
            "ORDER BY a.name LIMIT %s OFFSET %s",
            (search, search, search, limit, offset),
        )
        rows = cursor.fetchall()
    return {
        "items": [{"id": row[0], "name": row[1]} for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.get("/artists/{artist_id}")
def artist(artist_id: int, database: Database):
    with database.cursor() as cursor:
        cursor.execute("SELECT id, name FROM artists WHERE id = %s", (artist_id,))
        row = cursor.fetchone()
        if row is None:
            raise HTTPException(404, "Artist not found")
        cursor.execute(
            "SELECT alias FROM artist_aliases WHERE artist_id = %s ORDER BY alias",
            (artist_id,),
        )
        aliases = [record[0] for record in cursor.fetchall()]
        cursor.execute(
            f"SELECT {_CONCERT_COLUMNS} FROM concerts c JOIN concert_artists ca ON ca.concert_id = c.id "
            "WHERE ca.artist_id = %s AND c.starts_at >= now() AND c.status <> 'cancelled' "
            "ORDER BY c.starts_at LIMIT 100",
            (artist_id,),
        )
        shows = cursor.fetchall()
    return {
        "id": row[0],
        "name": row[1],
        "aliases": aliases,
        "concerts": [_concert_row(show) for show in shows],
    }


@app.post("/auth/telegram")
def telegram_login(request: TelegramLogin, database: Database):
    try:
        telegram = verify_init_data(request.init_data, telegram_bot_token())
    except InvalidInitData as exc:
        raise HTTPException(401, str(exc)) from exc
    return upsert_user(
        database, telegram.telegram_id, telegram.display_name, telegram.username
    )


@app.get("/me")
def me(database: Database, user_id: ProfileId):
    return get_profile(database, user_id)


@app.patch("/me")
def update_me(update: ProfileUpdate, database: Database, user_id: ProfileId):
    if update.city is not None and update.city not in {"Москва", "Санкт-Петербург"}:
        raise HTTPException(422, "Unsupported city")
    database.execute(
        "UPDATE users SET city = COALESCE(%s, city), notifications_enabled = COALESCE(%s, notifications_enabled) WHERE id = %s",
        (update.city, update.notifications_enabled, user_id),
    )
    return me(database, user_id)


@app.get("/me/subscriptions")
def subscriptions(database: Database, user_id: ProfileId):
    rows = database.execute(
        "SELECT a.id, a.name FROM user_artist_subscriptions s JOIN artists a ON a.id = s.artist_id "
        "WHERE s.user_id = %s ORDER BY a.name",
        (user_id,),
    ).fetchall()
    return [{"id": artist_id, "name": name} for artist_id, name in rows]


@app.put("/me/subscriptions/{artist_id}")
def subscribe(artist_id: int, database: Database, user_id: ProfileId):
    if (
        database.execute("SELECT 1 FROM artists WHERE id = %s", (artist_id,)).fetchone()
        is None
    ):
        raise HTTPException(404, "Artist not found")
    database.execute(
        "INSERT INTO user_artist_subscriptions (user_id, artist_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (user_id, artist_id),
    )
    return {"subscribed": True}


@app.delete("/me/subscriptions/{artist_id}")
def unsubscribe(artist_id: int, database: Database, user_id: ProfileId):
    database.execute(
        "DELETE FROM user_artist_subscriptions WHERE user_id = %s AND artist_id = %s",
        (user_id, artist_id),
    )
    return {"subscribed": False}


@app.get("/me/favorites")
def favorites(database: Database, user_id: ProfileId):
    rows = database.execute(
        f"SELECT {_CONCERT_COLUMNS} FROM concerts c JOIN user_favorites f ON f.concert_id = c.id "
        "WHERE f.user_id = %s AND EXISTS (SELECT 1 FROM research_source_events s "
        "WHERE s.concert_id = c.id AND s.classification = 'concert') ORDER BY c.starts_at",
        (user_id,),
    ).fetchall()
    return [_concert_row(row) for row in rows]


@app.put("/me/favorites/{concert_id}")
def favorite(concert_id: int, database: Database, user_id: ProfileId):
    row = database.execute(
        "SELECT starts_at FROM concerts c WHERE c.id = %s AND c.status <> 'cancelled' "
        "AND EXISTS (SELECT 1 FROM research_source_events s "
        "WHERE s.concert_id = c.id AND s.classification = 'concert')",
        (concert_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Concert not found")
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
    return {"favorite": True}


@app.delete("/me/favorites/{concert_id}")
def unfavorite(concert_id: int, database: Database, user_id: ProfileId):
    database.execute(
        "DELETE FROM user_favorites WHERE user_id = %s AND concert_id = %s",
        (user_id, concert_id),
    )
    database.execute(
        "DELETE FROM notification_outbox WHERE user_id = %s AND concert_id = %s "
        "AND kind = 'reminder' AND sent_at IS NULL",
        (user_id, concert_id),
    )
    return {"favorite": False}
