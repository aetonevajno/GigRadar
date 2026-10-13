import io
import os
import unittest
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from catalog import main
from normalize import normalize_kudago, normalize_timepad
from probe import ProbeError
from store import (
    StorageError,
    SyncOutcome,
    list_catalog_events,
    store_events,
    sync_timepad_city,
)

from app.concerts import find_concerts
from app.migration_runner import apply_migrations


class CatalogCommandTests(unittest.TestCase):
    def test_list_reads_database_without_source_requests(self):
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"DATABASE_URL": "postgresql://unused"}),
            patch("sys.argv", ["catalog.py", "list", "--city", "msk"]),
            patch("catalog.list_catalog_events", return_value=[]) as read_catalog,
            patch("catalog.collect_timepad") as fetch_timepad,
            patch("sys.stdout", output),
        ):
            self.assertEqual(main(), 0)

        self.assertEqual(output.getvalue().strip(), "[]")
        self.assertEqual(read_catalog.call_args.args[3], "Москва")
        fetch_timepad.assert_not_called()

    def test_watch_starts_sync_and_stops_cleanly(self):
        with (
            patch.dict(
                os.environ,
                {
                    "DATABASE_URL": "postgresql://unused",
                    "TIMEPAD_API_TOKEN": "unused",
                },
            ),
            patch("sys.argv", ["catalog.py", "watch", "--city", "msk"]),
            patch(
                "catalog.sync_timepad_city", return_value=SyncOutcome("fresh")
            ) as sync,
            patch("catalog.clock.sleep", side_effect=KeyboardInterrupt),
            self.assertLogs("gigradar.catalog", level="INFO") as recorded,
        ):
            self.assertEqual(main(), 0)

        self.assertEqual(sync.call_count, 1)
        self.assertIn("status=fresh", "\n".join(recorded.output))

    def test_catalog_read_failure_is_logged(self):
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"DATABASE_URL": "postgresql://unused"}),
            patch("sys.argv", ["catalog.py", "list"]),
            patch(
                "catalog.list_catalog_events", side_effect=StorageError("unavailable")
            ),
            patch("sys.stdout", output),
            self.assertLogs("gigradar.catalog", level="ERROR") as recorded,
        ):
            self.assertEqual(main(), 1)

        self.assertEqual(output.getvalue(), "")
        self.assertIn("Catalog read failed: unavailable", recorded.output[0])


class CatalogDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database_url = os.environ.get("TEST_DATABASE_URL")
        if not database_url:
            raise unittest.SkipTest("TEST_DATABASE_URL is not set")
        cls.base_url = database_url

    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        self.schema = f"gigradar_test_{uuid4().hex}"
        with psycopg.connect(self.base_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema))
            )
        self.database_url = make_conninfo(
            self.base_url, options=f"-c search_path={self.schema}"
        )

    def tearDown(self):
        import psycopg
        from psycopg import sql

        with psycopg.connect(self.base_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema))
            )

    def test_sync_skips_fresh_data_and_reads_updated_record(self):
        since = date(2026, 10, 9)
        until = date(2026, 11, 8)

        def event(title):
            return normalize_timepad(
                {
                    "id": 42,
                    "name": title,
                    "starts_at": "2026-10-20T19:00:00+03:00",
                    "location": {"city": "Москва"},
                    "url": "https://example.org/42",
                }
            )

        first = sync_timepad_city(
            self.database_url,
            "Москва",
            since,
            until,
            timedelta(hours=6),
            lambda: ([event("Первое название")], 0),
        )
        self.assertEqual((first.status, first.event_count), ("updated", 1))

        def unexpected_fetch():
            raise AssertionError("fresh sync must not call Timepad")

        second = sync_timepad_city(
            self.database_url,
            "Москва",
            since,
            until,
            timedelta(hours=6),
            unexpected_fetch,
        )
        self.assertEqual(second.status, "fresh")

        forced = sync_timepad_city(
            self.database_url,
            "Москва",
            since,
            until,
            timedelta(hours=6),
            lambda: ([event("Новое название")], 0),
            force=True,
        )
        self.assertEqual(forced.status, "updated")
        records = list_catalog_events(
            self.database_url,
            datetime(2026, 10, 9, tzinfo=UTC),
            datetime(2026, 11, 8, tzinfo=UTC),
            "Москва",
            100,
            0,
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["title"], "Новое название")
        self.assertEqual(records[0]["source_url"], "https://example.org/42")

        def failed_fetch():
            raise ProbeError("Timepad timed out")

        with self.assertRaises(ProbeError):
            sync_timepad_city(
                self.database_url,
                "Москва",
                since,
                until,
                timedelta(hours=6),
                failed_fetch,
                force=True,
            )
        self.assertEqual(
            list_catalog_events(
                self.database_url,
                datetime(2026, 10, 9, tzinfo=UTC),
                datetime(2026, 11, 8, tzinfo=UTC),
                "Москва",
                100,
                0,
            )[0]["title"],
            "Новое название",
        )

        with self.assertRaisesRegex(StorageError, "wrong-city"):
            sync_timepad_city(
                self.database_url,
                "Санкт-Петербург",
                since,
                until,
                timedelta(hours=6),
                lambda: ([event("Чужой город")], 0),
            )

    def test_parallel_city_sync_does_not_fetch_again(self):
        import psycopg

        city = "Санкт-Петербург"
        with psycopg.connect(self.database_url) as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"gigradar:timepad:{city}",),
            )
            outcome = sync_timepad_city(
                self.database_url,
                city,
                date(2026, 10, 9),
                date(2026, 11, 8),
                timedelta(hours=6),
                lambda: self.fail("parallel sync must not call Timepad"),
            )
        self.assertEqual(outcome.status, "busy")

    def test_missing_event_needs_two_complete_matching_windows(self):
        import psycopg

        start = datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=12)
        since = start.date()
        until = since + timedelta(days=30)

        def event(event_id):
            return normalize_timepad(
                {
                    "id": event_id,
                    "name": f"Концерт группы «Группа{event_id}»",
                    "starts_at": start.isoformat(),
                    "location": {"city": "Москва"},
                }
            )

        first, second, third, fourth, fifth = (
            event(event_id) for event_id in range(501, 506)
        )
        all_events = [first, second, third, fourth, fifth]
        without_first = [second, third, fourth, fifth]

        def sync(events, rejected=0, end=until, complete=True):
            return sync_timepad_city(
                self.database_url,
                "Москва",
                since,
                end,
                timedelta(0),
                lambda: (events, rejected),
                force=True,
                complete=complete,
            )

        sync(all_events)
        sync(without_first, rejected=1)
        sync(without_first, end=until + timedelta(days=1))
        with psycopg.connect(self.database_url) as database:
            self.assertEqual(
                database.execute(
                    "SELECT missing_count FROM research_source_events WHERE external_id = '501'"
                ).fetchone()[0],
                0,
            )
        sync(all_events, end=until)
        sync([second, third])
        sync([second, third])
        sync(without_first, complete=False)
        sync(without_first)
        with psycopg.connect(self.database_url) as database:
            self.assertEqual(
                find_concerts(database, city="Москва", query="Группа50")["total"], 5
            )
        sync(without_first)
        with psycopg.connect(self.database_url) as database:
            self.assertEqual(
                find_concerts(database, city="Москва", query="Группа50")["total"], 4
            )
            self.assertEqual(
                database.execute(
                    "SELECT is_active, missing_count FROM research_source_events "
                    "WHERE external_id = '501'"
                ).fetchone(),
                (False, 2),
            )
            self.assertEqual(
                database.execute(
                    "SELECT count(*) FROM concert_artists ca JOIN research_source_events s "
                    "ON s.concert_id = ca.concert_id WHERE s.external_id = '501'"
                ).fetchone()[0],
                0,
            )
        sync(all_events)
        with psycopg.connect(self.database_url) as database:
            self.assertEqual(
                find_concerts(database, city="Москва", query="Группа50")["total"], 5
            )
            self.assertEqual(
                database.execute(
                    "SELECT is_active, missing_count FROM research_source_events "
                    "WHERE external_id = '501'"
                ).fetchone(),
                (True, 0),
            )

    def test_upgrade_hides_prior_research_events_and_repairs_invalid_city(self):
        import psycopg

        starts_at = datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=10)
        experimental = normalize_kudago(
            {
                "id": 902,
                "title": "Концерт группы «Эксперимент»",
                "location": {"slug": "msk"},
                "dates": [{"start": int(starts_at.timestamp())}],
            }
        )
        store_events(self.database_url, [experimental])
        with psycopg.connect(self.database_url) as database:
            concert_id = database.execute(
                "SELECT concert_id FROM research_source_events WHERE source = 'kudago'"
            ).fetchone()[0]
            user_id = database.execute(
                "INSERT INTO users (telegram_id, display_name, city) "
                "VALUES (902, 'Test', 'Мосвка') RETURNING id"
            ).fetchone()[0]
            database.execute(
                "INSERT INTO user_favorites (user_id, concert_id) VALUES (%s, %s)",
                (user_id, concert_id),
            )
            database.execute(
                "INSERT INTO bot_searches (token, user_id, kind, query) "
                "VALUES ('legacy-search', %s, 'concert', 'Test')",
                (user_id,),
            )
            database.execute("DELETE FROM schema_migrations WHERE version = 9")
            apply_migrations(database.cursor())
            apply_migrations(database.cursor())
            self.assertEqual(
                database.execute(
                    "SELECT is_active FROM research_source_events WHERE source = 'kudago'"
                ).fetchone()[0],
                False,
            )
            self.assertIsNone(
                database.execute(
                    "SELECT city FROM users WHERE id = %s", (user_id,)
                ).fetchone()[0]
            )
            self.assertIsNotNone(
                database.execute(
                    "SELECT 1 FROM user_favorites WHERE user_id = %s", (user_id,)
                ).fetchone()
            )
            self.assertTrue(
                database.execute(
                    "SELECT bot_chat_available FROM users WHERE id = %s", (user_id,)
                ).fetchone()[0]
            )
            self.assertEqual(
                find_concerts(database, city="Москва", query="Эксперимент")["total"], 0
            )
