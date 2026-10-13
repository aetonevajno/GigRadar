from __future__ import annotations

import re

import psycopg
from aiogram.types import CallbackQuery, InlineQuery, Message

from app.profiles import mark_user_active, upsert_user

from .callbacks import callback_content, valid_offset
from .commands import command_response
from .localization import messages_for
from .views import inline_results


def parse_command(message: str, bot_username: str) -> tuple[str, str] | None:
    parts = message.strip().split(maxsplit=1)
    if not parts:
        return None
    command_text = parts[0]
    argument = parts[1] if len(parts) == 2 else ""
    match = re.fullmatch(r"/([a-z]+)(?:@([A-Za-z0-9_]+))?", command_text)
    if match is None:
        return None
    if match.group(2) and match.group(2).casefold() != bot_username.casefold():
        return None
    return match.group(1), argument.strip()


def message_content(
    database: psycopg.Connection,
    message: Message,
    bot_username: str,
    app_url: str | None,
) -> tuple[str, dict | None] | None:
    sender = message.from_user
    if (
        message.text is None
        or message.chat.type != "private"
        or sender is None
        or sender.id <= 0
        or message.chat.id != sender.id
        or not sender.first_name.strip()
    ):
        return None
    parsed = parse_command(message.text, bot_username)
    if parsed is None and message.text.lstrip().startswith("/"):
        return None
    command, argument = (
        parsed if parsed is not None else ("concerts", message.text.strip())
    )
    display_name = " ".join(
        part
        for part in (sender.first_name.strip(), (sender.last_name or "").strip())
        if part
    )
    profile = upsert_user(
        database, sender.id, display_name, sender.username, private_bot_chat=True
    )
    return command_response(database, profile, command, argument, app_url)


def callback_action(
    database: psycopg.Connection, callback: CallbackQuery
) -> tuple[tuple[str, dict | None] | None, str, bool]:
    answer = messages_for("ru")["action_retry"]
    message = callback.message
    if (
        not isinstance(message, Message)
        or callback.data is None
        or message.chat.type != "private"
        or message.chat.id != callback.from_user.id
    ):
        return None, answer, False
    user = database.execute(
        "SELECT id, locale FROM users WHERE telegram_id = %s", (callback.from_user.id,)
    ).fetchone()
    if user is None:
        return None, answer, False
    database.execute(
        "UPDATE users SET bot_chat_available = true WHERE id = %s", (user[0],)
    )
    mark_user_active(database, user[0])
    content, answer = callback_content(database, user[0], callback.data, user[1])
    return content, answer, callback.data.startswith("show:")


def inline_content(
    database: psycopg.Connection, query: InlineQuery
) -> tuple[list[dict], str]:
    offset = 0 if not query.offset else valid_offset(query.offset)
    city_row = database.execute(
        "SELECT id, city, locale FROM users WHERE telegram_id = %s",
        (query.from_user.id,),
    ).fetchone()
    city = city_row[1] if city_row else None
    if city_row is not None:
        mark_user_active(database, city_row[0])
    if len(query.query) > 100 or offset is None:
        return [], ""
    return inline_results(
        database, city, query.query, offset, city_row[2] if city_row else "ru"
    )
