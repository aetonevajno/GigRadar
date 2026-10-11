from __future__ import annotations

import re

import psycopg

from app.user_actions import (
    add_favorite,
    remove_favorite,
    subscribe_artist,
    unsubscribe_artist,
)

from .views import (
    artist_page,
    concert_detail,
    concert_page,
    favorites_page,
    load_search,
    subscriptions_page,
)


def valid_offset(raw: str) -> int | None:
    return int(raw) if raw.isdecimal() and len(raw) <= 5 and int(raw) <= 10000 else None


def valid_id(raw: str) -> int | None:
    return (
        int(raw)
        if raw.isdecimal() and len(raw) <= 19 and 0 < int(raw) < 2**63
        else None
    )


def callback_content(
    database: psycopg.Connection, user_id: int, callback_data: str
) -> tuple[tuple[str, dict | None] | None, str]:
    parts = callback_data.split(":")
    if len(parts) == 2 and parts[0] == "show":
        concert_id = valid_id(parts[1])
        if concert_id is not None:
            return concert_detail(database, user_id, concert_id), ""
    if len(parts) == 2 and parts[0] == "subs":
        offset = valid_offset(parts[1])
        if offset is not None:
            return subscriptions_page(database, user_id, offset), ""
    if len(parts) == 2 and parts[0] == "favorites":
        offset = valid_offset(parts[1])
        if offset is not None:
            return favorites_page(database, user_id, offset), ""
    if len(parts) == 4 and parts[0] == "page":
        kind, search_token, raw_offset = parts[1:]
        offset = valid_offset(raw_offset)
        if offset is not None and re.fullmatch(r"[A-Za-z0-9_-]{16}", search_token):
            search = (
                load_search(
                    database,
                    search_token,
                    user_id,
                    "concert" if kind == "c" else "artist",
                )
                if kind in {"c", "a"}
                else None
            )
            if search is not None:
                if kind == "c":
                    return concert_page(
                        database, search[1], search[0], offset, search_token
                    ), ""
                return artist_page(
                    database, user_id, search[0], offset, search_token
                ), ""
    if len(parts) == 3 and parts[0] == "fav" and parts[1] in {"add", "remove"}:
        concert_id = valid_id(parts[2])
        if concert_id is None:
            return None, "Действие недоступно."
        if parts[1] == "add":
            if not add_favorite(database, user_id, concert_id):
                return None, "Концерт больше не доступен."
            answer = "Добавлено в избранное"
        else:
            remove_favorite(database, user_id, concert_id)
            answer = "Убрано из избранного"
        return concert_detail(database, user_id, concert_id), answer
    if len(parts) == 5 and parts[0] == "sub" and parts[1] in {"add", "remove"}:
        artist_id = valid_id(parts[2])
        if artist_id is None:
            return None, "Действие недоступно."
        context, raw_offset = parts[3:]
        offset = valid_offset(raw_offset)
        if offset is None:
            return None, "Действие недоступно."
        artist_query = None
        artist_search_token = None
        concert_id = None
        if context.startswith("a"):
            artist_search_token = context[1:]
            search = (
                load_search(database, artist_search_token, user_id, "artist")
                if re.fullmatch(r"[A-Za-z0-9_-]{16}", artist_search_token)
                else None
            )
            if search is None:
                return None, "Поиск устарел. Выполните его заново."
            artist_query = search[0]
        elif context.startswith("c") and valid_id(context[1:]) is not None:
            concert_id = valid_id(context[1:])
        elif context != "s":
            return None, "Действие недоступно."
        if parts[1] == "add":
            if not subscribe_artist(database, user_id, artist_id):
                return None, "Артист больше не найден."
            answer = "Вы подписались на артиста"
        else:
            unsubscribe_artist(database, user_id, artist_id)
            answer = "Вы отписались от артиста"
        if artist_query is not None:
            return artist_page(
                database, user_id, artist_query, offset, artist_search_token
            ), answer
        if concert_id is not None:
            return concert_detail(database, user_id, concert_id), answer
        return subscriptions_page(database, user_id, offset), answer
    return None, "Действие недоступно. Выполните поиск заново."
