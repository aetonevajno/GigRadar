import io
import os
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from catalog import main
from normalize import normalize_timepad
from probe import ProbeError
from store import StorageError, SyncOutcome, list_catalog_events, sync_timepad_city


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
        output = io.StringIO()
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
            patch("sys.stdout", output),
        ):
            self.assertEqual(main(), 0)

        self.assertEqual(sync.call_count, 1)
        self.assertIn('"status": "fresh"', output.getvalue())


class CatalogDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database_url = os.environ.get("TEST_DATABASE_URL")
        if not database_url:
            raise unittest.SkipTest("TEST_DATABASE_URL is not set")
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        cls.schema = f"gigradar_test_{uuid4().hex}"
        cls.base_url = database_url
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema))
            )
        cls.database_url = make_conninfo(
            database_url, options=f"-c search_path={cls.schema}"
        )

    @classmethod
    def tearDownClass(cls):
        import psycopg
        from psycopg import sql

        with psycopg.connect(cls.base_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema))
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
            datetime(2026, 10, 9, tzinfo=timezone.utc),
            datetime(2026, 11, 8, tzinfo=timezone.utc),
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
                datetime(2026, 10, 9, tzinfo=timezone.utc),
                datetime(2026, 11, 8, tzinfo=timezone.utc),
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
