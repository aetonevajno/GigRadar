from __future__ import annotations

import psycopg

from app.cities import canonical_city

from .localization import messages_for
from .views import (
    artist_page,
    concert_page,
    create_search,
    favorites_page,
    subscriptions_page,
)

COMMAND_NAMES = (
    "start",
    "help",
    "concerts",
    "artists",
    "subscriptions",
    "favorites",
    "city",
    "language",
    "app",
)
PROFILE_PAGES = {"subscriptions": subscriptions_page, "favorites": favorites_page}


def commands_for(locale: str, app_url: str | None) -> list[dict[str, str]]:
    messages = messages_for(locale)
    return [
        {"command": name, "description": messages[f"command_{name}"]}
        for name in COMMAND_NAMES
        if app_url or name != "app"
    ]


def help_text(app_url: str | None, locale: str = "ru") -> str:
    messages = messages_for(locale)
    return (
        messages["help_heading"]
        + messages["help_commands"]
        + (messages["help_app"] if app_url else "")
        + messages["help_footer"]
    )


def app_button(app_url: str | None, locale: str = "ru") -> dict | None:
    if app_url is None:
        return None
    return {
        "inline_keyboard": [
            [{"text": messages_for(locale)["app_button"], "web_app": {"url": app_url}}]
        ]
    }


def command_response(
    database: psycopg.Connection,
    profile: dict,
    command: str,
    argument: str,
    app_url: str | None,
) -> tuple[str, dict | None]:
    locale = profile["locale"]
    messages = messages_for(locale)
    if command in {"concerts", "artists"}:
        if len(argument) > 100:
            reply, markup = messages["query_too_long"], None
        elif command == "concerts":
            search_token = create_search(
                database, profile["id"], "concert", argument, profile["city"]
            )
            reply, markup = concert_page(
                database, profile["city"], argument, 0, search_token, locale
            )
        elif not argument:
            reply, markup = messages["artist_query_prompt"], None
        else:
            search_token = create_search(database, profile["id"], "artist", argument)
            reply, markup = artist_page(
                database, profile["id"], argument, 0, search_token, locale
            )
    elif command in PROFILE_PAGES:
        reply, markup = PROFILE_PAGES[command](database, profile["id"], 0, locale)
    elif command == "city":
        if not argument:
            reply = messages["city_current"].format(
                city=profile["city"] or messages["all_cities"]
            )
        elif len(argument) > 80:
            reply = messages["city_too_long"]
        elif argument.casefold() != "all" and canonical_city(argument) is None:
            reply = messages["city_invalid"]
        else:
            city = None if argument.casefold() == "all" else canonical_city(argument)
            database.execute(
                "UPDATE users SET city = %s WHERE id = %s", (city, profile["id"])
            )
            reply = messages["city_saved"].format(city=city or messages["all_cities"])
        markup = None
    elif command == "language":
        if not argument:
            reply = messages["language_current"]
        elif argument not in {"ru", "en"}:
            reply = messages["language_invalid"]
        else:
            database.execute(
                "UPDATE users SET locale = %s WHERE id = %s", (argument, profile["id"])
            )
            reply = messages_for(argument)["language_saved"]
        markup = None
    else:
        if command == "start":
            database.execute(
                "UPDATE users SET notifications_enabled = true, bot_chat_available = true WHERE id = %s",
                (profile["id"],),
            )
        commands_help = help_text(app_url, locale)
        static_replies = {
            "start": messages["start_intro"] + commands_help,
            "help": commands_help,
            "app": messages["app_prompt"] if app_url else messages["app_unavailable"],
        }
        reply = static_replies.get(command, messages["unknown_command"] + commands_help)
        markup = app_button(app_url, locale) if command in {"start", "app"} else None
    return reply, markup
