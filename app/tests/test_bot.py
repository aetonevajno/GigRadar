import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from aiogram.types import CallbackQuery, InlineQuery, Message, Update

from bot.__main__ import run
from bot.handlers import on_callback, on_inline_query, on_message, router
from bot.service import callback_action, inline_content, message_content, parse_command
from bot.views import concert_page, source_url


def message(text="/start", *, chat_id=123, sender_id=123, chat_type="private"):
    return Message.model_validate(
        {
            "message_id": 9,
            "date": datetime.now(UTC),
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": sender_id, "is_bot": False, "first_name": "Анна"},
            "text": text,
        }
    )


def callback(data="fav:add:1", *, sender_id=123, chat_id=123):
    return CallbackQuery.model_validate(
        {
            "id": "callback-1",
            "from": {"id": sender_id, "is_bot": False, "first_name": "Анна"},
            "chat_instance": "test",
            "message": message(chat_id=chat_id).model_dump(by_alias=True),
            "data": data,
        }
    )


def inline_query(query="Мельница", offset=""):
    return InlineQuery.model_validate(
        {
            "id": "inline-1",
            "from": {"id": 123, "is_bot": False, "first_name": "Анна"},
            "query": query,
            "offset": offset,
        }
    )


class BotServiceTests(unittest.TestCase):
    def test_parses_private_bot_commands_and_search_terms(self):
        self.assertEqual(
            parse_command("/concerts@GigRadarBot\nМельница", "gigradarbot"),
            ("concerts", "Мельница"),
        )
        self.assertEqual(
            parse_command("/subscriptions", "gigradarbot"), ("subscriptions", "")
        )
        self.assertIsNone(parse_command("/concerts@OtherBot", "gigradarbot"))
        self.assertIsNone(parse_command("концерты", "gigradarbot"))

    def test_rejects_group_and_mismatched_sender_without_creating_profile(self):
        with patch("bot.service.upsert_user") as upsert:
            self.assertIsNone(
                message_content(
                    None,
                    message(chat_id=-100, chat_type="supergroup"),
                    "GigRadarBot",
                    None,
                )
            )
            self.assertIsNone(
                message_content(None, message(chat_id=456), "GigRadarBot", None)
            )
        upsert.assert_not_called()

    def test_start_search_and_plain_text_use_same_profile(self):
        with (
            patch(
                "bot.service.upsert_user", return_value={"id": 7, "city": "Москва"}
            ) as upsert,
            patch(
                "bot.service.command_response", return_value=("Найден концерт", None)
            ) as command,
        ):
            self.assertEqual(
                message_content(None, message(), "GigRadarBot", None),
                ("Найден концерт", None),
            )
            message_content(None, message("/concerts Мельница"), "GigRadarBot", None)
            command.assert_called_with(
                None, {"id": 7, "city": "Москва"}, "concerts", "Мельница", None
            )
            message_content(None, message("Мельница"), "GigRadarBot", None)
            command.assert_called_with(
                None, {"id": 7, "city": "Москва"}, "concerts", "Мельница", None
            )
        self.assertEqual(upsert.call_count, 3)

    def test_inline_query_uses_profile_city_and_rejects_invalid_offset(self):
        database = SimpleNamespace(
            execute=lambda *_: SimpleNamespace(fetchone=lambda: (7, "Москва"))
        )
        with (
            patch("bot.service.mark_user_active") as activity,
            patch(
                "bot.service.inline_results", return_value=([{"id": "1"}], "10")
            ) as search,
        ):
            self.assertEqual(
                inline_content(database, inline_query()), ([{"id": "1"}], "10")
            )
            search.assert_called_once_with(database, "Москва", "Мельница", 0)
            self.assertEqual(
                inline_content(database, inline_query(offset="bad")), ([], "")
            )
        self.assertEqual(activity.call_count, 2)

    def test_callback_rejects_other_user(self):
        with patch("bot.service.callback_content") as content:
            self.assertEqual(
                callback_action(None, callback(sender_id=999)),
                (None, "Действие недоступно. Выполните поиск заново.", False),
            )
        content.assert_not_called()

    def test_concert_page_navigation_keeps_search_token(self):
        def concert(concert_id):
            return {
                "id": concert_id,
                "title": f"Концерт {concert_id}",
                "starts_at": "2026-10-20T19:00:00+03:00",
                "city": "Москва",
                "venue": "Клуб",
                "sources": [],
            }

        with patch(
            "bot.views.find_concerts",
            return_value={"items": [concert(i) for i in range(1, 6)], "total": 12},
        ):
            text, markup = concert_page(
                None, "Москва", "Концерт", 0, "abcdefghijklmnop"
            )
        self.assertIn("Показаны 1–5 из 12", text)
        self.assertEqual(
            markup["inline_keyboard"][-1][0]["callback_data"],
            "page:c:abcdefghijklmnop:5",
        )

    def test_concert_source_button_accepts_only_valid_https_urls(self):
        concert = {
            "sources": [
                {"url": "https://bad.example/with space"},
                {"url": "https://user:pass@example.org/secret"},
                {"url": "https://tickets.example/concert"},
            ]
        }
        self.assertEqual(source_url(concert), "https://tickets.example/concert")


class BotHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatcher_routes_private_message(self):
        dispatcher = Dispatcher()
        dispatcher.include_router(router)
        update = Update(update_id=1, message=message("/help"))
        async with Bot("123:TEST") as bot:
            bot.send_message = AsyncMock()
            with patch("bot.handlers.with_database", return_value=("Справка", None)):
                await dispatcher.feed_update(
                    bot, update, bot_username="GigRadarBot", app_url=None
                )
            bot.send_message.assert_awaited_once_with(123, "Справка", reply_markup=None)

    async def test_message_sends_aiogram_keyboard(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        with patch(
            "bot.handlers.with_database",
            return_value=(
                "Привет",
                {
                    "inline_keyboard": [
                        [{"text": "Открыть", "web_app": {"url": "https://example.org"}}]
                    ]
                },
            ),
        ):
            await on_message(message(), bot, "GigRadarBot", "https://example.org")
        sent = bot.send_message.call_args
        self.assertEqual(sent.args, (123, "Привет"))
        self.assertEqual(
            sent.kwargs["reply_markup"].inline_keyboard[0][0].web_app.url,
            "https://example.org",
        )

    async def test_callback_answers_and_ignores_unchanged_edit(self):
        bot = SimpleNamespace(
            edit_message_text=AsyncMock(), answer_callback_query=AsyncMock()
        )
        bot.edit_message_text.side_effect = TelegramBadRequest(
            EditMessageText(text="Концерт", chat_id=123, message_id=9),
            "Bad Request: message is not modified",
        )
        with patch(
            "bot.handlers.with_database",
            return_value=(("Концерт", None), "Готово", False),
        ):
            await on_callback(callback(), bot)
        bot.answer_callback_query.assert_awaited_once_with("callback-1", text="Готово")

    async def test_inline_results_are_aiogram_articles(self):
        bot = SimpleNamespace(answer_inline_query=AsyncMock())
        article = {
            "type": "article",
            "id": "1",
            "title": "Концерт",
            "input_message_content": {"message_text": "Концерт завтра"},
        }
        with patch("bot.handlers.with_database", return_value=([article], "10")):
            await on_inline_query(inline_query(), bot)
        sent = bot.answer_inline_query.call_args
        self.assertEqual(sent.args[1][0].title, "Концерт")
        self.assertEqual(sent.kwargs["next_offset"], "10")

    async def test_poller_registers_commands_and_rejects_webhook(self):
        database = SimpleNamespace(
            execute=lambda *_: SimpleNamespace(fetchone=lambda: (True,))
        )
        bot = SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(username="GigRadarBot")),
            get_webhook_info=AsyncMock(return_value=SimpleNamespace(url="")),
            set_my_commands=AsyncMock(),
        )
        with patch("bot.__main__.Dispatcher") as dispatcher:
            dispatcher.return_value.start_polling = AsyncMock()
            await run(database, bot, None)
        self.assertEqual(
            bot.set_my_commands.call_args.kwargs["scope"].type, "all_private_chats"
        )
        self.assertEqual(
            dispatcher.return_value.start_polling.call_args.kwargs["allowed_updates"],
            ["message", "callback_query", "inline_query"],
        )
        self.assertFalse(
            dispatcher.return_value.start_polling.call_args.kwargs["handle_as_tasks"]
        )
        bot.get_webhook_info.return_value.url = "https://example.org/hook"
        with self.assertRaisesRegex(RuntimeError, "deleteWebhook"):
            await run(database, bot, None)


if __name__ == "__main__":
    unittest.main()
