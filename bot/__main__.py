from __future__ import annotations

import asyncio
import logging

import psycopg
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats

from app.config import database_url, telegram_app_url, telegram_bot_token
from app.telemetry import start_metrics_server

from .commands import COMMANDS
from .handlers import router

LOGGER = logging.getLogger("gigradar.bot")
POLL_LOCK_KEY = 1918879221


async def run(database: psycopg.Connection, bot: Bot, app_url: str | None) -> None:
    locked = database.execute(
        "SELECT pg_try_advisory_lock(%s)", (POLL_LOCK_KEY,)
    ).fetchone()[0]
    if not locked:
        raise RuntimeError("Another GigRadar bot poller is already running")
    identity = await bot.get_me()
    if not identity.username:
        raise RuntimeError("Telegram bot has no username")
    webhook = await bot.get_webhook_info()
    if webhook.url:
        raise RuntimeError(
            "Telegram webhook is configured; deleteWebhook before starting bot"
        )
    available_commands = [
        command for command in COMMANDS if app_url or command["command"] != "app"
    ]
    await bot.set_my_commands(
        [BotCommand.model_validate(command) for command in available_commands],
        scope=BotCommandScopeAllPrivateChats(),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    await dispatcher.start_polling(
        bot,
        handle_as_tasks=False,
        allowed_updates=["message", "callback_query", "inline_query"],
        close_bot_session=False,
        bot_username=identity.username,
        app_url=app_url,
    )


async def main() -> None:
    token = telegram_bot_token()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    app_url = telegram_app_url()
    start_metrics_server()
    with psycopg.connect(database_url(), autocommit=True) as database:
        async with Bot(token) as bot:
            await run(database, bot, app_url)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        LOGGER.info("Bot stopped")
