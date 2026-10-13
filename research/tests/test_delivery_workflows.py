import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
from app_store import review_artist_mention
from normalize import normalize_timepad
from notify import deliver_once
from psycopg import sql
from psycopg.conninfo import make_conninfo
from store import store_events

from app.user_actions import add_favorite, remove_favorite

MOSCOW_TIME = ZoneInfo("Europe/Moscow")


class DeliveryWorkflowTests(unittest.TestCase):
    def setUp(self):
        base_url = os.environ.get("TEST_DATABASE_URL")
        if not base_url:
            raise unittest.SkipTest("TEST_DATABASE_URL is not set")
        self.base_url = base_url
        self.schema = f"gigradar_delivery_{uuid4().hex}"
        with psycopg.connect(base_url, autocommit=True) as database:
            database.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema))
            )
        self.database_url = make_conninfo(
            base_url, options=f"-c search_path={self.schema}"
        )
        self.starts_at = (datetime.now(MOSCOW_TIME) + timedelta(days=7)).replace(
            hour=19, minute=0, second=0, microsecond=0
        )

    def tearDown(self):
        with psycopg.connect(self.base_url, autocommit=True) as database:
            database.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema))
            )

    def event(self, title, status=None, event_id=90001):
        payload = {
            "id": event_id,
            "name": title,
            "starts_at": self.starts_at.isoformat(),
            "location": {"city": "Москва", "address": "Клуб"},
        }
        if status:
            payload["status"] = status
        return normalize_timepad(payload)

    def user(self, database):
        return database.execute(
            "INSERT INTO users (telegram_id, display_name, notifications_enabled, "
            "bot_chat_available) VALUES (90001, 'Test', true, true) RETURNING id"
        ).fetchone()[0]

    def test_late_import_and_manual_review_notify_only_new_subscribers(self):
        store_events(self.database_url, [self.event("Концерт акустической музыки")])
        with psycopg.connect(self.database_url) as database:
            user_id = self.user(database)
            artist_id = database.execute(
                "INSERT INTO artists (name, name_key) VALUES ('Гамма', 'гамма') RETURNING id"
            ).fetchone()[0]
            database.execute(
                "INSERT INTO user_artist_subscriptions VALUES (%s, %s)",
                (user_id, artist_id),
            )
        linked = self.event("Концерт группы «Гамма»")
        store_events(self.database_url, [linked])
        store_events(self.database_url, [linked])
        with psycopg.connect(self.database_url) as database:
            self.assertEqual(
                database.execute(
                    "SELECT count(*) FROM notification_outbox WHERE user_id = %s "
                    "AND kind = 'new_concert'",
                    (user_id,),
                ).fetchone()[0],
                1,
            )

        store_events(
            self.database_url, [self.event("Концерт «Дельта»", event_id=90002)]
        )
        with psycopg.connect(self.database_url) as database:
            second_artist = database.execute(
                "INSERT INTO artists (name, name_key) VALUES ('Дельта', 'дельта') RETURNING id"
            ).fetchone()[0]
            database.execute(
                "INSERT INTO user_artist_subscriptions VALUES (%s, %s)",
                (user_id, second_artist),
            )
            mention_id = database.execute(
                "SELECT id FROM artist_mentions WHERE extracted_name = 'Дельта'"
            ).fetchone()[0]
            review_artist_mention(database.cursor(), mention_id, second_artist)
            review_artist_mention(database.cursor(), mention_id, second_artist)
            self.assertEqual(
                database.execute(
                    "SELECT count(*) FROM notification_outbox WHERE user_id = %s "
                    "AND kind = 'new_concert'",
                    (user_id,),
                ).fetchone()[0],
                2,
            )

    def test_expired_sending_becomes_uncertain_without_retry(self):
        store_events(self.database_url, [self.event("Концерт группы «Гамма»")])
        with psycopg.connect(self.database_url) as database:
            user_id = self.user(database)
            concert_id = database.execute("SELECT id FROM concerts").fetchone()[0]
            add_favorite(database, user_id, concert_id)
            notification_id = database.execute(
                "UPDATE notification_outbox SET delivery_state = 'sending', "
                "claimed_at = now() - interval '2 minutes', due_at = now() - interval '1 hour' "
                "WHERE kind = 'reminder' RETURNING id"
            ).fetchone()[0]
        with patch("notify.send_message") as send:
            self.assertEqual(deliver_once(self.database_url, "unused"), 0)
        send.assert_not_called()
        with psycopg.connect(self.database_url) as database:
            self.assertEqual(
                database.execute(
                    "SELECT delivery_state, failure_reason FROM notification_outbox WHERE id = %s",
                    (notification_id,),
                ).fetchone(),
                ("uncertain", "telegram"),
            )
            remove_favorite(database, user_id, concert_id)
            self.assertIsNotNone(
                database.execute(
                    "SELECT 1 FROM notification_outbox WHERE id = %s",
                    (notification_id,),
                ).fetchone()
            )

    def test_unfavorite_during_send_preserves_delivery_record(self):
        store_events(self.database_url, [self.event("Концерт группы «Гамма»")])
        with psycopg.connect(self.database_url) as database:
            user_id = self.user(database)
            concert_id = database.execute("SELECT id FROM concerts").fetchone()[0]
            add_favorite(database, user_id, concert_id)
            database.execute(
                "UPDATE notification_outbox SET due_at = now() - interval '1 minute'"
            )
        sending = threading.Event()
        release = threading.Event()
        removed = threading.Event()

        def send(_token, _pending):
            sending.set()
            if not release.wait(5):
                raise AssertionError("sender was not released")
            return 42

        def unfavorite():
            with psycopg.connect(self.database_url) as database:
                remove_favorite(database, user_id, concert_id)
            removed.set()

        with (
            patch("notify.send_message", side_effect=send),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            sender = executor.submit(deliver_once, self.database_url, "unused", limit=1)
            self.assertTrue(sending.wait(5))
            remover = executor.submit(unfavorite)
            self.assertFalse(removed.wait(0.1))
            release.set()
            self.assertEqual(sender.result(timeout=5), 1)
            remover.result(timeout=5)
        with psycopg.connect(self.database_url) as database:
            self.assertEqual(
                database.execute(
                    "SELECT delivery_state, telegram_message_id FROM notification_outbox "
                    "WHERE kind = 'reminder'"
                ).fetchone(),
                ("sent", 42),
            )
            self.assertIsNone(
                database.execute("SELECT 1 FROM user_favorites").fetchone()
            )

    def test_restoration_skips_stale_cancel_and_reports_delivered_cancel(self):
        scheduled = self.event("Концерт группы «Гамма»")
        cancelled = self.event("Концерт группы «Гамма»", "cancelled")
        store_events(self.database_url, [scheduled])
        with psycopg.connect(self.database_url) as database:
            user_id = self.user(database)
            concert_id = database.execute("SELECT id FROM concerts").fetchone()[0]
            add_favorite(database, user_id, concert_id)

        store_events(self.database_url, [cancelled])
        store_events(self.database_url, [scheduled])
        with patch("notify.send_message") as send:
            self.assertEqual(deliver_once(self.database_url, "unused"), 0)
        send.assert_not_called()
        with psycopg.connect(self.database_url) as database:
            self.assertEqual(
                database.execute(
                    "SELECT delivery_state FROM notification_outbox WHERE kind = 'cancelled'"
                ).fetchone()[0],
                "failed",
            )
            self.assertEqual(
                database.execute(
                    "SELECT count(*) FROM notification_outbox WHERE kind = 'restored'"
                ).fetchone()[0],
                0,
            )

        store_events(self.database_url, [cancelled])
        with patch("notify.send_message", return_value=77) as send:
            self.assertEqual(deliver_once(self.database_url, "unused"), 1)
        self.assertEqual(send.call_args.args[1].kind, "cancelled")
        store_events(self.database_url, [scheduled])
        with patch("notify.send_message", return_value=78) as send:
            self.assertEqual(deliver_once(self.database_url, "unused"), 1)
        self.assertEqual(send.call_args.args[1].kind, "restored")

    def test_restoration_requeues_ineligible_reminder(self):
        scheduled = self.event("Концерт группы «Гамма»")
        cancelled = self.event("Концерт группы «Гамма»", "cancelled")
        store_events(self.database_url, [scheduled])
        with psycopg.connect(self.database_url) as database:
            user_id = self.user(database)
            concert_id = database.execute("SELECT id FROM concerts").fetchone()[0]
            add_favorite(database, user_id, concert_id)
            database.execute(
                "UPDATE notification_outbox SET delivery_state = 'failed', "
                "failure_reason = 'ineligible' WHERE kind = 'reminder'"
            )
        store_events(self.database_url, [cancelled])
        store_events(self.database_url, [scheduled])
        with psycopg.connect(self.database_url) as database:
            self.assertEqual(
                database.execute(
                    "SELECT delivery_state, failure_reason FROM notification_outbox "
                    "WHERE kind = 'reminder'"
                ).fetchone(),
                ("queued", None),
            )

    def test_restoration_waits_for_in_flight_cancellation(self):
        scheduled = self.event("Концерт группы «Гамма»")
        cancelled = self.event("Концерт группы «Гамма»", "cancelled")
        store_events(self.database_url, [scheduled])
        with psycopg.connect(self.database_url) as database:
            user_id = self.user(database)
            concert_id = database.execute("SELECT id FROM concerts").fetchone()[0]
            add_favorite(database, user_id, concert_id)
        store_events(self.database_url, [cancelled])

        sending = threading.Event()
        release = threading.Event()
        restored = threading.Event()

        def send(_token, pending):
            self.assertEqual(pending.kind, "cancelled")
            sending.set()
            if not release.wait(5):
                raise AssertionError("sender was not released")
            return 81

        def restore():
            store_events(self.database_url, [scheduled])
            restored.set()

        with (
            patch("notify.send_message", side_effect=send),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            sender = executor.submit(deliver_once, self.database_url, "unused", limit=1)
            self.assertTrue(sending.wait(5))
            restorer = executor.submit(restore)
            self.assertFalse(restored.wait(0.1))
            release.set()
            self.assertEqual(sender.result(timeout=5), 1)
            restorer.result(timeout=5)

        with psycopg.connect(self.database_url) as database:
            self.assertEqual(
                database.execute(
                    "SELECT kind, delivery_state FROM notification_outbox "
                    "WHERE kind IN ('cancelled', 'restored') ORDER BY id"
                ).fetchall(),
                [("cancelled", "sent"), ("restored", "queued")],
            )


if __name__ == "__main__":
    unittest.main()
