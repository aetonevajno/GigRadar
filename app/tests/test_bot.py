import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from app.bot import BotApiError, handle_update, parse_command, run, telegram_request


class BotCommandTests(unittest.TestCase):
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

    def test_rejects_group_and_mismatched_sender_without_telegram_call(self):
        message = {
            "text": "/start",
            "chat": {"id": -100, "type": "supergroup"},
            "from": {"id": 123, "first_name": "Анна"},
        }
        with (
            patch(
                "app.bot.telegram_request", return_value={"message_id": 1}
            ) as request,
            patch("app.bot.upsert_user") as upsert,
        ):
            handle_update(
                None, None, "token", "GigRadarBot", None, {"message": message}
            )
            message["chat"] = {"id": 456, "type": "private"}
            handle_update(
                None, None, "token", "GigRadarBot", None, {"message": message}
            )
        request.assert_not_called()
        upsert.assert_not_called()

    def test_start_and_concert_search_send_chat_replies(self):
        message = {
            "text": "/start",
            "chat": {"id": 123, "type": "private"},
            "from": {"id": 123, "first_name": "Анна"},
        }
        with (
            patch(
                "app.bot.telegram_request", return_value={"message_id": 1}
            ) as request,
            patch("app.bot.upsert_user", return_value={"id": 7, "city": "Москва"}),
            patch("app.bot.concert_message", return_value="Найден концерт") as concerts,
        ):
            handle_update(
                None,
                None,
                "token",
                "GigRadarBot",
                "https://concerts.test/",
                {"message": message},
            )
            start_payload = request.call_args.args[3]
            self.assertEqual(start_payload["chat_id"], 123)
            self.assertEqual(
                start_payload["reply_markup"]["inline_keyboard"][0][0]["web_app"][
                    "url"
                ],
                "https://concerts.test/",
            )
            message["text"] = "/concerts Мельница"
            handle_update(
                None, None, "token", "GigRadarBot", None, {"message": message}
            )
            concerts.assert_called_once_with(None, "Москва", "Мельница")
            self.assertEqual(request.call_args.args[3]["text"], "Найден концерт")

    def test_telegram_failure_reports_code_without_exposing_token(self):
        def respond(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429,
                json={"ok": False, "error_code": 429, "parameters": {"retry_after": 8}},
            )

        with (
            httpx.Client(transport=httpx.MockTransport(respond)) as client,
            self.assertRaises(BotApiError) as raised,
        ):
            telegram_request(client, "private-token", "getUpdates", {"offset": 0})
        self.assertEqual(raised.exception.retry_after, 8)
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn("private-token", str(raised.exception))

    def test_poller_registers_commands_and_confirms_processed_updates(self):
        class Database:
            last_update_id = -1

            def execute(self, statement, parameters=()):
                if statement.startswith("SELECT pg_try_advisory_lock"):
                    return SimpleNamespace(fetchone=lambda: (True,))
                if statement.startswith("SELECT last_update_id"):
                    return SimpleNamespace(fetchone=lambda: (self.last_update_id,))
                if statement.startswith("UPDATE bot_poll_state"):
                    self.last_update_id = parameters[0] if parameters else -1
                    return None
                raise AssertionError(statement)

        database = Database()
        responses = iter(
            [{"username": "GigRadarBot"}, {"url": ""}, True, [{"update_id": 12}], []]
        )
        requests = []

        def request(_client, _token, method, payload):
            requests.append((method, payload))
            if len(requests) == 6:
                raise KeyboardInterrupt
            return next(responses)

        with (
            patch("app.bot.telegram_request", side_effect=request),
            patch("app.bot.handle_update") as handle,
            self.assertRaises(KeyboardInterrupt),
        ):
            run(database, None, "token", None)
        self.assertEqual(requests[2][0], "setMyCommands")
        self.assertEqual(requests[2][1]["scope"], {"type": "all_private_chats"})
        self.assertEqual([entry[1]["offset"] for entry in requests[3:]], [0, 13, 0])
        self.assertEqual(database.last_update_id, -1)
        handle.assert_called_once()

    def test_poller_rejects_existing_webhook(self):
        database = SimpleNamespace(
            execute=lambda _statement, _parameters: SimpleNamespace(
                fetchone=lambda: (True,)
            )
        )
        with (
            patch(
                "app.bot.telegram_request",
                side_effect=[
                    {"username": "GigRadarBot"},
                    {"url": "https://example.org/hook"},
                ],
            ) as request,
            self.assertRaisesRegex(RuntimeError, "deleteWebhook"),
        ):
            run(database, None, "token", None)
        self.assertEqual(request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
