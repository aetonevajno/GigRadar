from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import psycopg

from .concerts import find_concerts
from .config import database_url, telegram_app_url, telegram_bot_token
from .session import upsert_user

LOGGER = logging.getLogger("gigradar.bot")
MOSCOW_TIME = ZoneInfo("Europe/Moscow")
POLL_LOCK_KEY = 1918879221
COMMANDS = [
    {"command": "start", "description": "Начать работу с GigRadar"},
    {"command": "help", "description": "Показать команды"},
    {"command": "concerts", "description": "Ближайшие концерты или поиск"},
    {"command": "subscriptions", "description": "Мои подписки на артистов"},
    {"command": "app", "description": "Открыть приложение"},
]
HELP_TEXT = (
    "Команды GigRadar:\n"
    "/concerts — ближайшие концерты за 30 дней\n"
    "/concerts имя — поиск концерта или артиста\n"
    "/subscriptions — ваши подписки на артистов\n"
    "/app — открыть приложение\n"
    "/help — эта справка"
)
WELCOME_TEXT = (
    "Привет! Я помогу найти концерты и покажу ваших любимых артистов.\n\n" + HELP_TEXT
)


class BotApiError(Exception):
    def __init__(
        self, method: str, status: int, code: int | None, retry_after: int = 0
    ):
        super().__init__(f"Telegram {method} failed (HTTP {status}, code {code})")
        self.method = method
        self.status = status
        self.code = code
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.status in {0, 429} or self.status >= 500 or self.code == 429


def telegram_request(
    client: httpx.Client, token: str, method: str, payload: dict
) -> object:
    try:
        response = client.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=payload,
            timeout=35 if method == "getUpdates" else 10,
        )
    except httpx.RequestError as exc:
        raise BotApiError(method, 0, None) from exc
    try:
        body = response.json()
    except ValueError as exc:
        raise BotApiError(method, response.status_code, None) from exc
    if not isinstance(body, dict) or body.get("ok") is not True:
        code = body.get("error_code") if isinstance(body, dict) else None
        parameters = body.get("parameters") if isinstance(body, dict) else None
        retry_after = (
            parameters.get("retry_after", 0) if isinstance(parameters, dict) else 0
        )
        raise BotApiError(
            method,
            response.status_code,
            code if type(code) is int else None,
            retry_after if type(retry_after) is int and retry_after > 0 else 0,
        )
    if "result" not in body:
        raise BotApiError(method, response.status_code, None)
    return body["result"]


def app_button(app_url: str | None) -> dict | None:
    if app_url is None:
        return None
    return {
        "inline_keyboard": [[{"text": "Открыть GigRadar", "web_app": {"url": app_url}}]]
    }


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


def concert_message(database: psycopg.Connection, city: str | None, query: str) -> str:
    if len(query) > 100:
        return "Поисковый запрос слишком длинный. Сократите его до 100 символов."
    catalog = find_concerts(database, city=city, query=query, limit=5)
    if not catalog["items"]:
        return "Концертов по этому запросу в ближайшие 30 дней пока нет."
    heading = f"Ближайшие концерты{f' — {city}' if city else ''}:"
    lines = [heading]
    for show in catalog["items"]:
        starts_at = datetime.fromisoformat(show["starts_at"]).astimezone(MOSCOW_TIME)
        title = " ".join(show["title"].split())[:180]
        venue = " ".join((show["venue"] or "").split())[:80]
        city_name = " ".join(show["city"].split())[:80]
        location = f"{city_name}, {venue}" if venue else city_name
        show_text = f"{starts_at:%d.%m.%Y %H:%M} · {title}\n{location}"
        source_url = next(
            (
                source["url"]
                for source in show["sources"]
                if isinstance(source.get("url"), str)
                and source["url"].startswith("https://")
                and len(source["url"]) <= 200
            ),
            None,
        )
        lines.append(f"{show_text}\n{source_url}" if source_url else show_text)
    if catalog["total"] > len(catalog["items"]):
        lines.append(f"Показаны первые {len(catalog['items'])} из {catalog['total']}.")
    return "\n\n".join(lines)


def subscriptions_message(database: psycopg.Connection, user_id: int) -> str:
    rows = database.execute(
        "SELECT a.name FROM user_artist_subscriptions s JOIN artists a ON a.id = s.artist_id "
        "WHERE s.user_id = %s ORDER BY a.name LIMIT 21",
        (user_id,),
    ).fetchall()
    if not rows:
        return "Вы пока не подписаны на артистов. Добавить их можно в приложении."
    names = [
        f"{index}. {' '.join(row[0].split())[:100]}"
        for index, row in enumerate(rows[:20], 1)
    ]
    if len(rows) > 20:
        names.append("И другие подписки — в приложении.")
    return "Ваши подписки:\n" + "\n".join(names)


def handle_update(
    database: psycopg.Connection,
    client: httpx.Client,
    token: str,
    bot_username: str,
    app_url: str | None,
    update: dict,
) -> None:
    message = update.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("text"), str):
        return
    chat = message.get("chat")
    sender = message.get("from")
    if not isinstance(chat, dict) or not isinstance(sender, dict):
        return
    telegram_id = sender.get("id")
    if (
        chat.get("type") != "private"
        or type(telegram_id) is not int
        or telegram_id <= 0
        or chat.get("id") != telegram_id
    ):
        return
    parsed = parse_command(message["text"], bot_username)
    if parsed is None:
        return
    command, argument = parsed
    first_name = sender.get("first_name")
    if not isinstance(first_name, str) or not first_name.strip():
        return
    last_name = sender.get("last_name")
    username = sender.get("username")
    display_name = " ".join(
        part
        for part in (
            first_name.strip(),
            last_name.strip() if isinstance(last_name, str) else "",
        )
        if part
    )
    profile = upsert_user(
        database,
        telegram_id,
        display_name,
        username if isinstance(username, str) else None,
    )
    if command == "concerts":
        reply = concert_message(database, profile["city"], argument)
        markup = None
    elif command == "subscriptions":
        reply = subscriptions_message(database, profile["id"])
        markup = app_button(app_url)
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
    payload = {"chat_id": telegram_id, "text": reply}
    if markup is not None:
        payload["reply_markup"] = markup
    try:
        sent_message = telegram_request(client, token, "sendMessage", payload)
    except BotApiError as exc:
        if exc.code == 403:
            LOGGER.info(
                "Cannot reply to Telegram user %s: bot was blocked", telegram_id
            )
            return
        raise
    if (
        not isinstance(sent_message, dict)
        or type(sent_message.get("message_id")) is not int
    ):
        raise TypeError("Telegram sendMessage returned no message ID")


def run(
    database: psycopg.Connection, client: httpx.Client, token: str, app_url: str | None
) -> None:
    locked = database.execute(
        "SELECT pg_try_advisory_lock(%s)", (POLL_LOCK_KEY,)
    ).fetchone()[0]
    if not locked:
        raise RuntimeError("Another GigRadar bot poller is already running")
    identity = telegram_request(client, token, "getMe", {})
    if not isinstance(identity, dict) or not isinstance(identity.get("username"), str):
        raise TypeError("Telegram getMe returned no bot username")
    bot_username = identity["username"]
    webhook = telegram_request(client, token, "getWebhookInfo", {})
    if not isinstance(webhook, dict) or not isinstance(webhook.get("url"), str):
        raise TypeError("Telegram getWebhookInfo returned an invalid result")
    if webhook["url"]:
        raise RuntimeError(
            "Telegram webhook is configured; deleteWebhook before starting bot"
        )
    command_registration = telegram_request(
        client,
        token,
        "setMyCommands",
        {"commands": COMMANDS, "scope": {"type": "all_private_chats"}},
    )
    if command_registration is not True:
        raise TypeError("Telegram setMyCommands returned an invalid result")
    last_update_id = database.execute(
        "SELECT last_update_id FROM bot_poll_state WHERE singleton = true"
    ).fetchone()[0]
    while True:
        try:
            updates = telegram_request(
                client,
                token,
                "getUpdates",
                {
                    "offset": last_update_id + 1,
                    "timeout": 25,
                    "allowed_updates": ["message"],
                },
            )
            if not isinstance(updates, list):
                raise TypeError("Telegram getUpdates returned invalid updates")
            if not updates and last_update_id >= 0:
                database.execute(
                    "UPDATE bot_poll_state SET last_update_id = -1 WHERE singleton = true"
                )
                last_update_id = -1
            for update in updates:
                if (
                    not isinstance(update, dict)
                    or type(update.get("update_id")) is not int
                ):
                    raise TypeError("Telegram getUpdates returned an invalid update")
                update_id = update["update_id"]
                handle_update(database, client, token, bot_username, app_url, update)
                database.execute(
                    "UPDATE bot_poll_state SET last_update_id = %s WHERE singleton = true",
                    (update_id,),
                )
                last_update_id = update_id
        except BotApiError as exc:
            if not exc.retryable:
                raise
            LOGGER.warning("%s; retrying", exc)
            time.sleep(min(max(exc.retry_after, 5), 60))


def main() -> None:
    token = telegram_bot_token()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    app_url = telegram_app_url()
    with (
        psycopg.connect(database_url(), autocommit=True) as database,
        httpx.Client() as client,
    ):
        run(database, client, token, app_url)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        main()
    except KeyboardInterrupt:
        LOGGER.info("Bot stopped")
    except BotApiError as exc:
        LOGGER.error("%s", exc)
        raise SystemExit(1) from None
