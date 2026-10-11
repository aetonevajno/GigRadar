from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated

import psycopg
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .artist_catalog import find_artists
from .artist_discovery import router as artist_discovery_router
from .auth import InvalidInitData, verify_init_data
from .concerts import CONCERT_COLUMNS, concert_row, find_concerts
from .config import cors_origins, telegram_bot_token
from .profiles import upsert_user
from .session import Database, ProfileId, database_url, get_profile
from .user_actions import (
    add_favorite,
    remove_favorite,
    subscribe_artist,
    unsubscribe_artist,
)
from .web_routes import router as web_auth_router

RESEARCH_DIR = Path(__file__).resolve().parents[1] / "research"


@asynccontextmanager
async def lifespan(_: FastAPI):
    with psycopg.connect(database_url()) as connection:
        connection.execute((RESEARCH_DIR / "schema.sql").read_text(encoding="utf-8"))
        for version, filename in (
            (1, "001_app.sql"),
            (2, "002_delivery.sql"),
            (3, "003_web_auth.sql"),
            (4, "004_artist_catalog.sql"),
            (5, "005_bot_poll_state.sql"),
            (6, "006_bot_searches.sql"),
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
app.include_router(artist_discovery_router)
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
    return find_concerts(
        database,
        city=city,
        from_date=from_date,
        to_date=to_date,
        artist_id=artist_id,
        query=q,
        limit=limit,
        offset=offset,
    )


@app.get("/concerts/{concert_id}")
def concert(concert_id: int, database: Database):
    with database.cursor() as cursor:
        cursor.execute(
            f"SELECT {CONCERT_COLUMNS} FROM concerts c WHERE c.id = %s AND EXISTS "
            "(SELECT 1 FROM research_source_events s WHERE s.concert_id = c.id AND s.classification = 'concert')",
            (concert_id,),
        )
        row = cursor.fetchone()
    if row is None:
        raise HTTPException(404, "Concert not found")
    return concert_row(row)


@app.get("/artists")
def artists(
    database: Database,
    q: str | None = Query(None, max_length=100),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    return find_artists(database, q, limit=limit, offset=offset)


@app.get("/artists/{artist_id}")
def artist(artist_id: int, database: Database):
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT id, name, musicbrainz_id FROM artists WHERE id = %s", (artist_id,)
        )
        row = cursor.fetchone()
        if row is None:
            raise HTTPException(404, "Artist not found")
        cursor.execute(
            "SELECT alias FROM artist_aliases WHERE artist_id = %s ORDER BY alias",
            (artist_id,),
        )
        aliases = [record[0] for record in cursor.fetchall()]
        cursor.execute(
            f"SELECT {CONCERT_COLUMNS} FROM concerts c JOIN concert_artists ca ON ca.concert_id = c.id "
            "WHERE ca.artist_id = %s AND c.starts_at >= now() AND c.status <> 'cancelled' "
            "ORDER BY c.starts_at LIMIT 100",
            (artist_id,),
        )
        shows = cursor.fetchall()
    return {
        "id": row[0],
        "name": row[1],
        "musicbrainz_url": f"https://musicbrainz.org/artist/{row[2]}"
        if row[2]
        else None,
        "aliases": aliases,
        "concerts": [concert_row(show) for show in shows],
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
    if not subscribe_artist(database, user_id, artist_id):
        raise HTTPException(404, "Artist not found")
    return {"subscribed": True}


@app.delete("/me/subscriptions/{artist_id}")
def unsubscribe(artist_id: int, database: Database, user_id: ProfileId):
    unsubscribe_artist(database, user_id, artist_id)
    return {"subscribed": False}


@app.get("/me/favorites")
def favorites(database: Database, user_id: ProfileId):
    rows = database.execute(
        f"SELECT {CONCERT_COLUMNS} FROM concerts c JOIN user_favorites f ON f.concert_id = c.id "
        "WHERE f.user_id = %s AND EXISTS (SELECT 1 FROM research_source_events s "
        "WHERE s.concert_id = c.id AND s.classification = 'concert') ORDER BY c.starts_at",
        (user_id,),
    ).fetchall()
    return [concert_row(row) for row in rows]


@app.put("/me/favorites/{concert_id}")
def favorite(concert_id: int, database: Database, user_id: ProfileId):
    if not add_favorite(database, user_id, concert_id):
        raise HTTPException(404, "Concert not found")
    return {"favorite": True}


@app.delete("/me/favorites/{concert_id}")
def unfavorite(concert_id: int, database: Database, user_id: ProfileId):
    remove_favorite(database, user_id, concert_id)
    return {"favorite": False}
