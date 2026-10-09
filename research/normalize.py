from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

MOSCOW_TIME = ZoneInfo("Europe/Moscow")
CITY_NAMES = {"msk": "Москва", "spb": "Санкт-Петербург"}


@dataclass(frozen=True)
class Event:
    source: str
    external_id: str
    title: str
    city: str | None
    starts_at: datetime | None
    venue: str | None
    url: str | None
    artist_candidates: tuple[str, ...]
    payload: dict[str, Any]


def _string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _local_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(MOSCOW_TIME)


def normalize_kudago(
    payload: dict[str, Any], since: date | None = None, until: date | None = None
) -> Event:
    location = payload.get("location")
    slug = location.get("slug") if isinstance(location, dict) else location
    city = CITY_NAMES.get(slug)
    place = payload.get("place")
    venue = _string(place.get("title")) if isinstance(place, dict) else None
    starts = []
    for occurrence in payload.get("dates") or []:
        if not isinstance(occurrence, dict):
            continue
        timestamp = occurrence.get("start")
        if isinstance(timestamp, int) and timestamp > 0:
            local_start = datetime.fromtimestamp(timestamp, timezone.utc).astimezone(
                MOSCOW_TIME
            )
            if (since is None or local_start.date() >= since) and (
                until is None or local_start.date() < until
            ):
                starts.append(local_start)
    participants = payload.get("participants") or []
    artists = []
    for participant in participants:
        if not isinstance(participant, dict):
            continue
        agent = participant.get("agent")
        role = participant.get("role")
        if not isinstance(agent, dict) or not isinstance(role, dict):
            continue
        role_name = str(role.get("slug") or role.get("name") or "").lower()
        if not any(
            word in role_name for word in ("music", "singer", "band", "dj", "performer")
        ):
            continue
        name = _string(agent.get("title"))
        if name:
            artists.append(name)
    starts_at = min(starts) if starts else None
    return Event(
        source="kudago",
        external_id=str(payload["id"]),
        title=_string(payload.get("title")) or "",
        city=city,
        starts_at=starts_at,
        venue=venue,
        url=_string(payload.get("site_url")),
        artist_candidates=tuple(dict.fromkeys(artists)),
        payload=payload,
    )


def normalize_timepad(payload: dict[str, Any]) -> Event:
    location = payload.get("location")
    city_name = _string(location.get("city")) if isinstance(location, dict) else None
    city = city_name if city_name in CITY_NAMES.values() else None
    venue = _string(location.get("address")) if isinstance(location, dict) else None
    return Event(
        source="timepad",
        external_id=str(payload["id"]),
        title=_string(payload.get("name")) or "",
        city=city,
        starts_at=_local_time(payload.get("starts_at")),
        venue=venue,
        url=_string(payload.get("url")),
        artist_candidates=(),
        payload=payload,
    )


def duplicate_candidates(events: list[Event]) -> list[dict[str, str]]:
    groups: dict[tuple[str, str, str], list[Event]] = {}
    for event in events:
        if not event.city or not event.starts_at or not event.title:
            continue
        title = re.sub(r"[^\w]+", " ", html.unescape(event.title).casefold()).strip()
        key = (event.city, event.starts_at.date().isoformat(), title)
        groups.setdefault(key, []).append(event)
    matches = []
    for group in groups.values():
        for first_index, first in enumerate(group):
            for second in group[first_index + 1 :]:
                if first.source == second.source and (
                    first.external_id == second.external_id
                    or first.starts_at != second.starts_at
                ):
                    continue
                matches.append(
                    {
                        "first": f"{first.source}:{first.external_id}",
                        "second": f"{second.source}:{second.external_id}",
                    }
                )
    return matches


def quality_metrics(events: list[Event]) -> dict[str, object]:
    total = len(events)
    complete = sum(
        event.city is not None and event.starts_at is not None for event in events
    )
    structured_artists = sum(bool(event.artist_candidates) for event in events)
    return {
        "events": total,
        "sources_with_events": len({event.source for event in events}),
        "city_and_date_present_percent": round(100 * complete / total, 1)
        if total
        else None,
        "structured_artist_candidate_percent": round(
            100 * structured_artists / total, 1
        )
        if total
        else None,
        "artist_identification_accuracy_percent": None,
        "duplicate_candidates": duplicate_candidates(events),
    }
