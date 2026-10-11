from __future__ import annotations

import psycopg

from .views import (
    artist_page,
    concert_page,
    create_search,
    favorites_page,
    subscriptions_page,
)

COMMANDS = [
    {"command": "start", "description": "Начать работу с GigRadar"},
    {"command": "help", "description": "Показать команды"},
    {"command": "concerts", "description": "Ближайшие концерты или поиск"},
    {"command": "artists", "description": "Найти артиста и подписаться"},
    {"command": "subscriptions", "description": "Мои подписки на артистов"},
    {"command": "favorites", "description": "Избранные концерты"},
    {"command": "city", "description": "Выбрать город"},
    {"command": "app", "description": "Открыть приложение"},
]
HELP_TEXT = (
    "Команды GigRadar:\n"
    "/concerts — ближайшие концерты за 30 дней\n"
    "/concerts имя — поиск концерта или артиста\n"
    "Или просто отправьте название концерта или артиста\n"
    "/artists имя — найти артиста и подписаться\n"
    "/subscriptions — посмотреть подписки и отписаться\n"
    "/favorites — избранные концерты\n"
    "/city город — показывать концерты выбранного города\n"
    "/city all — концерты всех городов\n"
    "/app — открыть приложение\n"
    "/help — справка"
)
WELCOME_TEXT = (
    "Привет! Я помогу найти концерты и покажу ваших любимых артистов.\n\n" + HELP_TEXT
)


def app_button(app_url: str | None) -> dict | None:
    if app_url is None:
        return None
    return {
        "inline_keyboard": [[{"text": "Открыть GigRadar", "web_app": {"url": app_url}}]]
    }


def command_response(
    database: psycopg.Connection,
    profile: dict,
    command: str,
    argument: str,
    app_url: str | None,
) -> tuple[str, dict | None]:
    if command in {"concerts", "artists"}:
        if len(argument) > 100:
            reply, markup = (
                "Поисковый запрос слишком длинный. Сократите его до 100 символов.",
                None,
            )
        elif command == "concerts":
            search_token = create_search(
                database, profile["id"], "concert", argument, profile["city"]
            )
            reply, markup = concert_page(
                database, profile["city"], argument, 0, search_token
            )
        elif not argument:
            reply, markup = "Напишите /artists имя, чтобы найти артиста.", None
        else:
            search_token = create_search(database, profile["id"], "artist", argument)
            reply, markup = artist_page(
                database, profile["id"], argument, 0, search_token
            )
    elif command == "subscriptions":
        reply, markup = subscriptions_page(database, profile["id"], 0)
    elif command == "favorites":
        reply, markup = favorites_page(database, profile["id"], 0)
    elif command == "city":
        if not argument:
            reply = f"Текущий город: {profile['city'] or 'все города'}. Укажите /city город или /city all."
        elif len(argument) > 80:
            reply = "Название города слишком длинное."
        else:
            city = None if argument.casefold() == "all" else argument
            if city is not None:
                known_city = database.execute(
                    "SELECT city FROM concerts WHERE lower(city) = lower(%s) ORDER BY city LIMIT 1",
                    (city,),
                ).fetchone()
                if known_city is not None:
                    city = known_city[0]
            database.execute(
                "UPDATE users SET city = %s WHERE id = %s", (city, profile["id"])
            )
            reply = f"Теперь показываю концерты: {city or 'все города'}."
        markup = None
    else:
        static_replies = {
            "start": WELCOME_TEXT,
            "help": HELP_TEXT,
            "app": (
                "Откройте GigRadar, чтобы управлять подписками, избранным и настройками."
                if app_url
                else "Приложение пока не настроено."
            ),
        }
        reply = static_replies.get(command, "Неизвестная команда.\n\n" + HELP_TEXT)
        markup = app_button(app_url) if command in {"start", "app"} else None
    return reply, markup
