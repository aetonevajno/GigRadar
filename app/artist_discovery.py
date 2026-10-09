from __future__ import annotations

import logging
import re
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Query
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .session import Database, ProfileId

router = APIRouter(prefix="/artists/discover")
LOGGER = logging.getLogger(__name__)
MUSICBRAINZ_URL = "https://musicbrainz.org/ws/2/artist/"
USER_AGENT = "GigRadar/0.1 (https://t.me/GigRadarAppBot)"


class ArtistSelection(BaseModel):
    query: str = Field(min_length=3, max_length=100)
    musicbrainz_id: UUID


def _search_key(query: str) -> str:
    return re.sub(r"\s+", " ", query.casefold()).strip()


def _candidates(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        raise HTTPException(502, "Artist directory returned invalid data")
    artists = payload.get("artists")
    if not isinstance(artists, list):
        raise HTTPException(502, "Artist directory returned invalid data")
    candidates = []
    for artist in artists:
        if not isinstance(artist, dict):
            continue
        try:
            musicbrainz_id = str(UUID(artist["id"]))
        except (KeyError, TypeError, ValueError):
            continue
        name = artist.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            continue
        candidates.append(
            {
                "musicbrainz_id": musicbrainz_id,
                "name": name.strip(),
                "description": str(artist.get("disambiguation") or "")[:200],
                "url": f"https://musicbrainz.org/artist/{musicbrainz_id}",
            }
        )
    return candidates


@router.get("")
def search_directory(database: Database, q: str = Query(min_length=3, max_length=100)):
    query_key = _search_key(q)
    if len(query_key) < 3:
        raise HTTPException(422, "Enter at least three characters")
    cached = database.execute(
        "SELECT candidates FROM artist_search_cache "
        "WHERE query_key = %s AND fetched_at > now() - interval '7 days'",
        (query_key,),
    ).fetchone()
    if cached is not None:
        return {"items": cached[0]}

    reserved = database.execute(
        "UPDATE artist_search_rate SET next_request_at = now() + interval '1 second' "
        "WHERE singleton AND next_request_at <= now() RETURNING singleton"
    ).fetchone()
    if reserved is None:
        raise HTTPException(429, "Artist directory is busy; try again shortly")
    database.commit()

    try:
        response = httpx.get(
            MUSICBRAINZ_URL,
            params={"query": q.strip(), "fmt": "json", "limit": 10},
            headers={"User-Agent": USER_AGENT},
            timeout=8,
        )
        response.raise_for_status()
        candidates = _candidates(response.json())
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        LOGGER.warning("MusicBrainz artist lookup failed: %s", exc)
        raise HTTPException(503, "Artist directory is temporarily unavailable") from exc
    except httpx.HTTPStatusError as exc:
        LOGGER.warning(
            "MusicBrainz artist lookup returned %s", exc.response.status_code
        )
        status = 503 if exc.response.status_code in {429, 500, 502, 503, 504} else 502
        raise HTTPException(
            status, "Artist directory is temporarily unavailable"
        ) from exc
    except ValueError as exc:
        LOGGER.warning("MusicBrainz artist lookup returned invalid JSON")
        raise HTTPException(502, "Artist directory returned invalid data") from exc

    database.execute(
        "INSERT INTO artist_search_cache (query_key, candidates, fetched_at) "
        "VALUES (%s, %s, now()) ON CONFLICT (query_key) DO UPDATE SET "
        "candidates = EXCLUDED.candidates, fetched_at = EXCLUDED.fetched_at",
        (query_key, Jsonb(candidates)),
    )
    return {"items": candidates}


@router.post("")
def add_discovered_artist(selection: ArtistSelection, database: Database, _: ProfileId):
    query_key = _search_key(selection.query)
    cached = database.execute(
        "SELECT candidates FROM artist_search_cache "
        "WHERE query_key = %s AND fetched_at > now() - interval '7 days'",
        (query_key,),
    ).fetchone()
    if cached is None:
        raise HTTPException(404, "Search again before selecting an artist")
    candidate = next(
        (
            artist
            for artist in cached[0]
            if artist["musicbrainz_id"] == str(selection.musicbrainz_id)
        ),
        None,
    )
    if candidate is None:
        raise HTTPException(404, "Artist was not in these search results")

    existing = database.execute(
        "SELECT id, name FROM artists WHERE musicbrainz_id = %s",
        (selection.musicbrainz_id,),
    ).fetchone()
    if existing is not None:
        return {"id": existing[0], "name": existing[1]}
    name_key = _search_key(candidate["name"])
    inserted = database.execute(
        "INSERT INTO artists (name, name_key, musicbrainz_id) VALUES (%s, %s, %s) "
        "ON CONFLICT (name_key) DO NOTHING RETURNING id, name",
        (candidate["name"], name_key, selection.musicbrainz_id),
    ).fetchone()
    if inserted is None:
        existing = database.execute(
            "SELECT id, name FROM artists WHERE musicbrainz_id = %s",
            (selection.musicbrainz_id,),
        ).fetchone()
        if existing is not None:
            return {"id": existing[0], "name": existing[1]}
        raise HTTPException(409, "An artist with this name is already in the catalog")
    return {"id": inserted[0], "name": inserted[1]}
