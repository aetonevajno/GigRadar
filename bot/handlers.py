from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from time import monotonic

import psycopg
from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    Message,
)

from app.config import database_url
from app.telemetry import BOT_DURATION, BOT_REQUESTS

from .service import callback_action, inline_content, message_content, parse_command

LOGGER = logging.getLogger("gigradar.bot")
router = Router()


def with_database[T](operation: Callable[..., T], *args: object) -> T:
    with psycopg.connect(database_url(), autocommit=True) as database:
        return operation(database, *args)


def keyboard(markup: dict | None) -> InlineKeyboardMarkup | None:
    return InlineKeyboardMarkup.model_validate(markup) if markup is not None else None


async def send_reply(bot: Bot, chat_id: int, text: str, markup: dict | None) -> None:
    try:
        await bot.send_message(chat_id, text, reply_markup=keyboard(markup))
    except TelegramForbiddenError:
        LOGGER.info("Cannot reply to Telegram user %s: bot was blocked", chat_id)


@router.message()
async def on_message(
    message: Message, bot: Bot, bot_username: str, app_url: str | None
) -> None:
    parsed = parse_command(message.text or "", bot_username)
    command = (
        parsed[0] if parsed and parsed[0] in {
            "start",
            "help",
            "concerts",
            "artists",
            "subscriptions",
            "favorites",
            "city",
            "app",
        }
        else "search"
    )
    started = monotonic()
    try:
        response = await asyncio.to_thread(
            with_database, message_content, message, bot_username, app_url
        )
        if response is not None:
            await send_reply(bot, message.chat.id, *response)
    except Exception:
        BOT_REQUESTS.labels(command, "error").inc()
        raise
    else:
        BOT_REQUESTS.labels(command, "ok" if response is not None else "ignored").inc()
    finally:
        BOT_DURATION.labels(command).observe(monotonic() - started)


@router.callback_query()
async def on_callback(callback: CallbackQuery, bot: Bot) -> None:
    command = (callback.data or "").partition(":")[0]
    if command not in {"fav", "sub", "show", "page", "subs", "favorites"}:
        command = "callback_other"
    started = monotonic()
    try:
        content, answer, show = await asyncio.to_thread(
            with_database, callback_action, callback
        )
        if content is not None and isinstance(callback.message, Message):
            text, markup = content
            if show:
                await send_reply(bot, callback.message.chat.id, text, markup)
            else:
                try:
                    await bot.edit_message_text(
                        text,
                        chat_id=callback.message.chat.id,
                        message_id=callback.message.message_id,
                        reply_markup=keyboard(markup),
                    )
                except TelegramBadRequest as exc:
                    if "message is not modified" not in str(exc).lower():
                        raise
        await bot.answer_callback_query(callback.id, text=answer)
    except Exception:
        BOT_REQUESTS.labels(command, "error").inc()
        raise
    else:
        BOT_REQUESTS.labels(command, "ok").inc()
    finally:
        BOT_DURATION.labels(command).observe(monotonic() - started)


@router.inline_query()
async def on_inline_query(query: InlineQuery, bot: Bot) -> None:
    started = monotonic()
    try:
        articles, next_offset = await asyncio.to_thread(
            with_database, inline_content, query
        )
        await bot.answer_inline_query(
            query.id,
            [InlineQueryResultArticle.model_validate(article) for article in articles],
            next_offset=next_offset,
            cache_time=5,
            is_personal=True,
        )
    except Exception:
        BOT_REQUESTS.labels("inline", "error").inc()
        raise
    else:
        BOT_REQUESTS.labels("inline", "ok").inc()
    finally:
        BOT_DURATION.labels("inline").observe(monotonic() - started)
