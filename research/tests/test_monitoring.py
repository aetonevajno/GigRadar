import json
import os
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from io import BytesIO
from unittest.mock import Mock, patch
from urllib.error import URLError
from uuid import uuid4

import psycopg
from normalize import normalize_timepad
from notify import (
    DeliveryFailure,
    PendingMessage,
    claim_next,
    deliver_once,
    send_message,
)
from psycopg import sql
from psycopg.conninfo import make_conninfo
from store import store_events

from monitoring.exporter import DatabaseSnapshot


class DatabaseSnapshotTests(unittest.TestCase):
    def test_missing_city_is_distinct_from_zero_age(self):
        database = Mock()
        database.execute.side_effect = [
            Mock(fetchall=lambda: [("Москва", 0)]),
            Mock(fetchone=lambda: (0, 0, 0)),
        ]

        @contextmanager
        def connect(**_):
            yield database

        with (
            patch.dict(
                os.environ,
                {
                    "PGHOST": "db",
                    "PGDATABASE": "app",
                    "PGUSER": "monitor",
                    "PGPASSWORD": "test",
                },
            ),
            patch("monitoring.exporter.psycopg.connect", side_effect=connect),
        ):
            metrics = list(DatabaseSnapshot().collect())

        samples = {
            sample.name: sample
            for metric in metrics
            for sample in metric.samples
            if sample.labels.get("city") == "Санкт-Петербург"
        }
        self.assertEqual(samples["gigradar_import_state_present"].value, 0)
        self.assertNotIn("gigradar_import_success_age_seconds", samples)
        self.assertEqual(metrics[0].samples[0].value, 1)

    def test_database_error_emits_health_failure_without_old_state(self):
        with patch(
            "monitoring.exporter.psycopg.connect", side_effect=KeyError("PGHOST")
        ):
            metrics = list(DatabaseSnapshot().collect())
        self.assertEqual([metric.name for metric in metrics], ["gigradar_database_up"])
        self.assertEqual(metrics[0].samples[0].value, 0)

    def test_api_health_failure_is_visible_separately(self):
        with (
            patch.dict(os.environ, {"API_HEALTH_URL": "http://api:8000/health"}),
            patch("monitoring.exporter.urlopen", side_effect=URLError("offline")),
            patch(
                "monitoring.exporter.psycopg.connect", side_effect=KeyError("PGHOST")
            ),
        ):
            metrics = list(DatabaseSnapshot().collect())
        self.assertEqual(
            [metric.name for metric in metrics],
            ["gigradar_api_health_up", "gigradar_database_up"],
        )
        self.assertEqual([metric.samples[0].value for metric in metrics], [0, 0])


class NotificationLocalizationTests(unittest.TestCase):
    def test_english_notification_uses_profile_language(self):
        payloads = []

        def telegram_response(request, timeout):
            self.assertEqual(timeout, 10)
            payloads.append(json.loads(request.data))
            return BytesIO(b'{"ok":true,"result":{"message_id":12}}')

        pending = PendingMessage(
            1,
            123,
            "reminder",
            "Test concert",
            datetime(2026, 10, 20, 16, tzinfo=UTC),
            "Москва",
            "Club",
            "https://tickets.example/concert",
            "en",
        )
        with patch("notify.urlopen", side_effect=telegram_response):
            self.assertEqual(send_message("test-token", pending), 12)

        self.assertIn("Concert reminder", payloads[0]["text"])
        self.assertIn("20.10.2026 at 19:00", payloads[0]["text"])
        self.assertEqual(
            payloads[0]["reply_markup"]["inline_keyboard"][0][0]["text"], "Open source"
        )


class NotificationReasonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base_url = os.environ.get("TEST_DATABASE_URL")
        if not base_url:
            raise unittest.SkipTest("TEST_DATABASE_URL is not set")
        cls.base_url = base_url
        cls.schema = f"gigradar_notification_test_{uuid4().hex}"
        with psycopg.connect(base_url, autocommit=True) as database:
            database.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema))
            )
        cls.database_url = make_conninfo(
            base_url, options=f"-c search_path={cls.schema}"
        )
        starts_at = datetime.now(UTC) + timedelta(days=7)
        event = normalize_timepad(
            {
                "id": 991,
                "name": "Концерт тестового артиста",
                "starts_at": starts_at.isoformat(),
                "location": {"city": "Москва"},
            }
        )
        store_events(cls.database_url, [event])

    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(cls.base_url, autocommit=True) as database:
            database.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema))
            )

    def test_ineligible_and_telegram_failures_have_distinct_reasons(self):
        with psycopg.connect(self.database_url) as database:
            concert_id = database.execute("SELECT id FROM concerts").fetchone()[0]
            user_id = database.execute(
                "INSERT INTO users (telegram_id, display_name) VALUES (991, 'Test') RETURNING id"
            ).fetchone()[0]
            skipped_id = database.execute(
                "INSERT INTO notification_outbox (user_id, concert_id, kind, dedupe_key, due_at) "
                "VALUES (%s, %s, 'reminder', 'skipped', now() - interval '1 hour') RETURNING id",
                (user_id, concert_id),
            ).fetchone()[0]
        self.assertIsNone(claim_next(self.database_url))
        with psycopg.connect(self.database_url) as database:
            self.assertEqual(
                database.execute(
                    "SELECT delivery_state, failure_reason FROM notification_outbox WHERE id=%s",
                    (skipped_id,),
                ).fetchone(),
                ("failed", "ineligible"),
            )
            database.execute(
                "INSERT INTO user_favorites (user_id, concert_id) VALUES (%s, %s)",
                (user_id, concert_id),
            )
            database.execute(
                "UPDATE users SET locale = 'en', notifications_enabled = true, "
                "bot_chat_available = true WHERE id = %s",
                (user_id,),
            )
            failed_id = database.execute(
                "INSERT INTO notification_outbox (user_id, concert_id, kind, dedupe_key, due_at) "
                "VALUES (%s, %s, 'reminder', 'telegram-failure', now() - interval '1 hour') RETURNING id",
                (user_id, concert_id),
            ).fetchone()[0]
        with patch(
            "notify.send_message",
            side_effect=DeliveryFailure(
                "Telegram HTTP 400", "failed", outcome="http_4xx"
            ),
        ) as send:
            self.assertEqual(deliver_once(self.database_url, "unused", limit=1), 0)
        self.assertEqual(send.call_args.args[1].locale, "en")
        with psycopg.connect(self.database_url) as database:
            self.assertEqual(
                database.execute(
                    "SELECT delivery_state, failure_reason FROM notification_outbox WHERE id=%s",
                    (failed_id,),
                ).fetchone(),
                ("failed", "telegram"),
            )


if __name__ == "__main__":
    unittest.main()
