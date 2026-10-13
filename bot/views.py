from __future__ import annotations

import secrets
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import psycopg

from app.artist_catalog import find_artists
from app.concerts import CONCERT_COLUMNS, concert_row, find_concerts

from .localization import messages_for

MOSCOW_TIME = ZoneInfo("Europe/Moscow")
CONCERT_PAGE_SIZE = 5
ARTIST_PAGE_SIZE = 8
SUBSCRIPTION_PAGE_SIZE = 10


def source_url(concert: dict) -> str | None:
    for source in concert["sources"]:
        url = source.get("url")
        if (
            not isinstance(url, str)
            or len(url) > 200
            or any(ord(char) < 33 for char in url)
        ):
            continue
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme == "https"
                and parsed.hostname
                and not parsed.username
                and not parsed.password
            ):
                return url
        except ValueError:
            continue
    return None


def concert_summary(concert: dict) -> str:
    starts_at = datetime.fromisoformat(concert["starts_at"]).astimezone(MOSCOW_TIME)
    title = " ".join(concert["title"].split())[:180]
    venue = " ".join((concert["venue"] or "").split())[:80]
    city = " ".join(concert["city"].split())[:80]
    location = f"{city}, {venue}" if venue else city
    summary = f"{starts_at:%d.%m.%Y %H:%M} · {title}\n{location}"
    url = source_url(concert)
    return f"{summary}\n{url}" if url else summary


def create_search(
    database: psycopg.Connection,
    user_id: int,
    kind: str,
    query: str,
    city: str | None = None,
) -> str:
    token = secrets.token_urlsafe(12)
    database.execute(
        "DELETE FROM bot_searches WHERE created_at < now() - interval '1 day'"
    )
    database.execute(
        "INSERT INTO bot_searches (token, user_id, kind, query, city) VALUES (%s, %s, %s, %s, %s)",
        (token, user_id, kind, query, city),
    )
    return token


def load_search(
    database: psycopg.Connection, token: str, user_id: int, kind: str
) -> tuple[str, str | None] | None:
    row = database.execute(
        "SELECT query, city FROM bot_searches WHERE token = %s AND user_id = %s "
        "AND kind = %s AND created_at >= now() - interval '1 day'",
        (token, user_id, kind),
    ).fetchone()
    return (row[0], row[1]) if row else None


def concert_page(
    database: psycopg.Connection,
    city: str | None,
    query: str,
    offset: int,
    token: str | None,
    locale: str = "ru",
) -> tuple[str, dict | None]:
    messages = messages_for(locale)
    catalog = find_concerts(
        database, city=city, query=query, limit=CONCERT_PAGE_SIZE, offset=offset
    )
    if not catalog["items"]:
        return messages["no_concerts"], None
    heading = messages["concert_heading"].format(city=f" — {city}" if city else "")
    lines = [heading]
    buttons = []
    for number, concert in enumerate(catalog["items"], offset + 1):
        lines.append(f"{number}. {concert_summary(concert)}")
        buttons.append(
            [
                {
                    "text": f"{number}. {messages['concert_details']}",
                    "callback_data": f"show:{concert['id']}",
                }
            ]
        )
    lines.append(
        messages["shown"].format(
            first=offset + 1,
            last=offset + len(catalog["items"]),
            total=catalog["total"],
        )
    )
    if token:
        navigation = []
        if offset:
            navigation.append(
                {
                    "text": messages["back"],
                    "callback_data": f"page:c:{token}:{max(0, offset - CONCERT_PAGE_SIZE)}",
                }
            )
        if offset + CONCERT_PAGE_SIZE < catalog["total"]:
            navigation.append(
                {
                    "text": messages["next"],
                    "callback_data": f"page:c:{token}:{offset + CONCERT_PAGE_SIZE}",
                }
            )
        if navigation:
            buttons.append(navigation)
    return "\n\n".join(lines), {"inline_keyboard": buttons}


def artist_page(
    database: psycopg.Connection,
    user_id: int,
    query: str,
    offset: int,
    token: str,
    locale: str = "ru",
) -> tuple[str, dict | None]:
    messages = messages_for(locale)
    catalog = find_artists(database, query, limit=ARTIST_PAGE_SIZE, offset=offset)
    if not catalog["items"]:
        return messages["no_artists"], None
    artist_ids = [artist["id"] for artist in catalog["items"]]
    rows = database.execute(
        "SELECT artist_id FROM user_artist_subscriptions WHERE user_id = %s AND artist_id = ANY(%s)",
        (user_id, artist_ids),
    ).fetchall()
    subscribed = {row[0] for row in rows}
    buttons = []
    lines = [messages["artists_heading"]]
    for number, artist in enumerate(catalog["items"], offset + 1):
        name = " ".join(artist["name"].split())[:100]
        lines.append(f"{number}. {name}")
        action = "remove" if artist["id"] in subscribed else "add"
        label = messages["unfollow"] if action == "remove" else messages["follow"]
        buttons.append(
            [
                {
                    "text": f"{number}. {label}",
                    "callback_data": f"sub:{action}:{artist['id']}:a{token}:{offset}",
                }
            ]
        )
    navigation = []
    if offset:
        navigation.append(
            {
                "text": messages["back"],
                "callback_data": f"page:a:{token}:{max(0, offset - ARTIST_PAGE_SIZE)}",
            }
        )
    if offset + ARTIST_PAGE_SIZE < catalog["total"]:
        navigation.append(
            {
                "text": messages["next"],
                "callback_data": f"page:a:{token}:{offset + ARTIST_PAGE_SIZE}",
            }
        )
    if navigation:
        buttons.append(navigation)
    return "\n".join(lines), {"inline_keyboard": buttons}


def subscriptions_page(
    database: psycopg.Connection, user_id: int, offset: int, locale: str = "ru"
) -> tuple[str, dict | None]:
    messages = messages_for(locale)
    total = database.execute(
        "SELECT count(*) FROM user_artist_subscriptions WHERE user_id = %s", (user_id,)
    ).fetchone()[0]
    if not total:
        return messages["no_subscriptions"], None
    offset = min(
        offset, ((total - 1) // SUBSCRIPTION_PAGE_SIZE) * SUBSCRIPTION_PAGE_SIZE
    )
    rows = database.execute(
        "SELECT a.id, a.name FROM user_artist_subscriptions s JOIN artists a ON a.id = s.artist_id "
        "WHERE s.user_id = %s ORDER BY a.name, a.id LIMIT %s OFFSET %s",
        (user_id, SUBSCRIPTION_PAGE_SIZE, offset),
    ).fetchall()
    lines = [messages["subscriptions_heading"]]
    buttons = []
    for number, (artist_id, name) in enumerate(rows, offset + 1):
        lines.append(f"{number}. {' '.join(name.split())[:100]}")
        buttons.append(
            [
                {
                    "text": f"{number}. {messages['unfollow']}",
                    "callback_data": f"sub:remove:{artist_id}:s:{offset}",
                }
            ]
        )
    navigation = []
    if offset:
        navigation.append(
            {
                "text": messages["back"],
                "callback_data": f"subs:{max(0, offset - SUBSCRIPTION_PAGE_SIZE)}",
            }
        )
    if offset + SUBSCRIPTION_PAGE_SIZE < total:
        navigation.append(
            {
                "text": messages["next"],
                "callback_data": f"subs:{offset + SUBSCRIPTION_PAGE_SIZE}",
            }
        )
    if navigation:
        buttons.append(navigation)
    return "\n".join(lines), {"inline_keyboard": buttons}


def favorites_page(
    database: psycopg.Connection, user_id: int, offset: int, locale: str = "ru"
) -> tuple[str, dict | None]:
    messages = messages_for(locale)
    filters = (
        "FROM concerts c JOIN user_favorites f ON f.concert_id = c.id "
        "WHERE f.user_id = %s AND EXISTS (SELECT 1 FROM research_source_events s "
        "WHERE s.concert_id = c.id AND s.classification = 'concert')"
    )
    total = database.execute(f"SELECT count(*) {filters}", (user_id,)).fetchone()[0]
    if not total:
        return messages["no_favorites"], None
    offset = min(offset, ((total - 1) // CONCERT_PAGE_SIZE) * CONCERT_PAGE_SIZE)
    rows = database.execute(
        f"SELECT {CONCERT_COLUMNS} {filters} ORDER BY c.starts_at, c.id LIMIT %s OFFSET %s",
        (user_id, CONCERT_PAGE_SIZE, offset),
    ).fetchall()
    lines = [messages["favorites_heading"]]
    buttons = []
    for number, row in enumerate(rows, offset + 1):
        concert = concert_row(row)
        lines.append(f"{number}. {concert_summary(concert)}")
        buttons.append(
            [
                {
                    "text": f"{number}. {messages['concert_details']}",
                    "callback_data": f"show:{concert['id']}",
                }
            ]
        )
    navigation = []
    if offset:
        navigation.append(
            {
                "text": messages["back"],
                "callback_data": f"favorites:{max(0, offset - CONCERT_PAGE_SIZE)}",
            }
        )
    if offset + CONCERT_PAGE_SIZE < total:
        navigation.append(
            {
                "text": messages["next"],
                "callback_data": f"favorites:{offset + CONCERT_PAGE_SIZE}",
            }
        )
    if navigation:
        buttons.append(navigation)
    return "\n\n".join(lines), {"inline_keyboard": buttons}


def concert_detail(
    database: psycopg.Connection, user_id: int, concert_id: int, locale: str = "ru"
) -> tuple[str, dict | None]:
    messages = messages_for(locale)
    row = database.execute(
        f"SELECT {CONCERT_COLUMNS} FROM concerts c WHERE c.id = %s AND EXISTS "
        "(SELECT 1 FROM research_source_events s WHERE s.concert_id = c.id AND s.classification = 'concert')",
        (concert_id,),
    ).fetchone()
    if row is None:
        return messages["concert_missing"], None
    concert = concert_row(row)
    lines = [concert_summary(concert)]
    if concert["status"] == "cancelled":
        lines.insert(0, messages["concert_cancelled"])
    elif concert["status"] == "postponed":
        lines.insert(0, messages["concert_postponed"])
    if concert["artists"]:
        lines.append(
            messages["artists_label"]
            + ", ".join(artist["name"] for artist in concert["artists"][:5])
        )
    favorite = (
        database.execute(
            "SELECT 1 FROM user_favorites WHERE user_id = %s AND concert_id = %s",
            (user_id, concert_id),
        ).fetchone()
        is not None
    )
    buttons = []
    if favorite or concert["status"] != "cancelled":
        favorite_action = "remove" if favorite else "add"
        buttons.append(
            [
                {
                    "text": messages["remove_favorite"]
                    if favorite
                    else messages["add_favorite"],
                    "callback_data": f"fav:{favorite_action}:{concert_id}",
                }
            ]
        )
    if concert["artists"]:
        artist_ids = [artist["id"] for artist in concert["artists"][:5]]
        rows = database.execute(
            "SELECT artist_id FROM user_artist_subscriptions WHERE user_id = %s AND artist_id = ANY(%s)",
            (user_id, artist_ids),
        ).fetchall()
        subscribed = {row[0] for row in rows}
        for artist in concert["artists"][:5]:
            action = "remove" if artist["id"] in subscribed else "add"
            label = (
                messages["unfollow_artist"]
                if action == "remove"
                else messages["follow_artist"]
            )
            buttons.append(
                [
                    {
                        "text": f"{label} {artist['name'][:35]}",
                        "callback_data": f"sub:{action}:{artist['id']}:c{concert_id}:0",
                    }
                ]
            )
    url = source_url(concert)
    if url:
        buttons.append([{"text": messages["source_tickets"], "url": url}])
    return "\n\n".join(lines)[:4096], {"inline_keyboard": buttons}


def inline_results(
    database: psycopg.Connection,
    city: str | None,
    query: str,
    offset: int,
    locale: str = "ru",
) -> tuple[list[dict], str]:
    messages = messages_for(locale)
    catalog = find_concerts(database, city=city, query=query, limit=10, offset=offset)
    results = []
    for concert in catalog["items"]:
        starts_at = datetime.fromisoformat(concert["starts_at"]).astimezone(MOSCOW_TIME)
        url = source_url(concert)
        article = {
            "type": "article",
            "id": str(concert["id"]),
            "title": concert["title"][:100],
            "description": f"{concert['city']} · {starts_at:%d.%m.%Y %H:%M}"[:200],
            "input_message_content": {"message_text": concert_summary(concert)},
        }
        if url:
            article["reply_markup"] = {
                "inline_keyboard": [[{"text": messages["source_tickets"], "url": url}]]
            }
        results.append(article)
    next_offset = str(offset + 10) if offset + 10 < catalog["total"] else ""
    return results, next_offset
