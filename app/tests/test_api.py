import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
from app_store import add_artist_alias, backfill, review_artist_mention
from fastapi.testclient import TestClient
from normalize import normalize_kudago, normalize_timepad
from notify import deliver_once
from psycopg import sql
from psycopg.conninfo import make_conninfo
from store import store_events

from app.main import app
from app.tests.test_auth import signed_data
from app.web_auth import InvalidIdentity, ProviderUnavailable, WebIdentity, token_hash

MOSCOW_TIME = ZoneInfo("Europe/Moscow")
OIDC_TEST_ENV = {
    "TELEGRAM_OIDC_CLIENT_ID": "12345",
    "TELEGRAM_OIDC_CLIENT_SECRET": "test-secret",
    "TELEGRAM_OIDC_REDIRECT_URI": "https://concerts.test/api/auth/web/callback",
}


def concert(
    event_id: int,
    title: str,
    days_ahead: int,
    *,
    venue: str = "Клуб",
    status: str | None = None,
):
    starts_at = (datetime.now(MOSCOW_TIME) + timedelta(days=days_ahead)).replace(
        hour=19, minute=0, second=0, microsecond=0
    )
    payload = {
        "id": event_id,
        "name": title,
        "starts_at": starts_at.isoformat(),
        "location": {"city": "Москва", "address": venue},
        "url": f"https://example.org/{event_id}",
    }
    if status:
        payload["status"] = status
    return normalize_timepad(payload)


class ApiDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database_url = os.environ.get("TEST_DATABASE_URL")
        if not database_url:
            raise unittest.SkipTest("TEST_DATABASE_URL is not set")
        cls.schema = f"gigradar_api_test_{uuid4().hex}"
        cls.base_url = database_url
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema))
            )
        cls.database_url = make_conninfo(
            database_url, options=f"-c search_path={cls.schema}"
        )
        cls.environment = patch.dict(
            os.environ,
            {"DATABASE_URL": cls.database_url, "TELEGRAM_BOT_TOKEN": "test-token"},
        )
        cls.environment.start()
        cls.client = TestClient(app)
        cls.client.__enter__()
        store_events(
            cls.database_url,
            [
                concert(1001, "Концерт группы «Тест»", 7),
                concert(1002, "Стендап и шутки", 7),
            ],
        )

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.environment.stop()
        with psycopg.connect(cls.base_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema))
            )

    def test_feed_filters_nonconcerts_and_empty_search(self):
        response = self.client.get("/concerts?city=Москва")
        self.assertEqual(response.status_code, 200)
        titles = [entry["title"] for entry in response.json()["items"]]
        self.assertIn("Концерт группы «Тест»", titles)
        self.assertNotIn("Стендап и шутки", titles)
        self.assertEqual(response.json()["items"][0]["sources"][0]["source"], "timepad")
        self.assertEqual(
            self.client.get("/concerts?q=несуществующее").json()["total"], 0
        )
        self.assertEqual(self.client.get("/concerts/999999").status_code, 404)

    def test_backfill_normalizes_existing_titles(self):
        event = concert(1202, "Концерт &quot;Тишина&quot;", 10)
        store_events(self.database_url, [event])
        with psycopg.connect(self.database_url) as connection:
            concert_id = connection.execute(
                "SELECT concert_id FROM research_source_events WHERE external_id = '1202'"
            ).fetchone()[0]
            connection.execute(
                "UPDATE research_source_events SET title = %s WHERE external_id = '1202'",
                ("Концерт &quot;Тишина&quot;",),
            )
            connection.execute(
                "UPDATE concerts SET title = %s WHERE id = %s",
                ("Концерт &quot;Тишина&quot;", concert_id),
            )
        backfill(self.database_url)
        with psycopg.connect(self.database_url) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT title FROM research_source_events WHERE external_id = '1202'"
                ).fetchone()[0],
                'Концерт "Тишина"',
            )
        self.assertEqual(
            self.client.get(f"/concerts/{concert_id}").json()["title"],
            'Концерт "Тишина"',
        )

    def test_users_are_isolated_and_invalid_data_rejected(self):
        first = signed_data(
            111, "test-token", int(datetime.now(timezone.utc).timestamp())
        )
        second = signed_data(
            222, "test-token", int(datetime.now(timezone.utc).timestamp())
        )
        self.assertEqual(
            self.client.post("/auth/telegram", json={"init_data": "bad"}).status_code,
            401,
        )
        self.assertEqual(
            self.client.post("/auth/telegram", json={"init_data": first}).status_code,
            200,
        )
        self.assertEqual(
            self.client.post("/auth/telegram", json={"init_data": second}).status_code,
            200,
        )
        first_headers = {"X-Telegram-Init-Data": first}
        second_headers = {"X-Telegram-Init-Data": second}
        artist_id = self.client.get("/artists?q=Тест").json()["items"][0]["id"]
        concert_id = self.client.get("/concerts?city=Москва").json()["items"][0]["id"]
        self.assertEqual(
            self.client.put(
                f"/me/subscriptions/{artist_id}", headers=first_headers
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.put(
                f"/me/favorites/{concert_id}", headers=first_headers
            ).status_code,
            200,
        )
        self.assertEqual(
            len(self.client.get("/me/subscriptions", headers=first_headers).json()), 1
        )
        self.assertEqual(
            self.client.get("/me/subscriptions", headers=second_headers).json(), []
        )
        self.assertEqual(
            self.client.get("/me/favorites", headers=second_headers).json(), []
        )
        self.assertEqual(
            self.client.patch(
                "/me", headers=first_headers, json={"city": "Москва"}
            ).json()["city"],
            "Москва",
        )
        self.assertIsNone(self.client.get("/me", headers=second_headers).json()["city"])

    def test_import_changes_and_delivery_are_idempotent(self):
        artist_id = self.client.get("/artists?q=Тест").json()["items"][0]["id"]
        signed = signed_data(
            333, "test-token", int(datetime.now(timezone.utc).timestamp())
        )
        self.client.post("/auth/telegram", json={"init_data": signed})
        headers = {"X-Telegram-Init-Data": signed}
        self.client.put(f"/me/subscriptions/{artist_id}", headers=headers)
        store_events(self.database_url, [concert(1001, "Концерт группы «Тест»", 7)])
        with psycopg.connect(self.database_url) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM notification_outbox WHERE kind = 'new_concert' AND user_id = (SELECT id FROM users WHERE telegram_id = 333)"
                ).fetchone()[0],
                0,
            )
        second = concert(1003, "Концерт группы «Тест»", 14)
        store_events(self.database_url, [second])
        store_events(self.database_url, [second])
        with psycopg.connect(self.database_url) as connection:
            count = connection.execute(
                "SELECT count(*) FROM notification_outbox WHERE kind = 'new_concert' AND user_id = (SELECT id FROM users WHERE telegram_id = 333)"
            ).fetchone()[0]
        self.assertEqual(count, 1)
        with patch("notify.send_message", return_value=42) as send:
            self.assertEqual(deliver_once(self.database_url, "unused"), 1)
            self.assertEqual(deliver_once(self.database_url, "unused"), 0)
        self.assertEqual(send.call_count, 1)
        moved = concert(1003, "Концерт группы «Тест»", 15)
        store_events(self.database_url, [moved])
        store_events(self.database_url, [moved])
        with psycopg.connect(self.database_url) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM concert_changes WHERE change_type = 'rescheduled'"
                ).fetchone()[0],
                1,
            )
        cancelled = concert(1003, "Концерт группы «Тест»", 15, status="cancelled")
        store_events(self.database_url, [cancelled])
        store_events(self.database_url, [])
        with psycopg.connect(self.database_url) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM concerts WHERE id = (SELECT concert_id FROM research_source_events WHERE external_id = '1003')"
                ).fetchone()[0],
                "cancelled",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM concert_changes WHERE change_type = 'cancelled'"
                ).fetchone()[0],
                1,
            )

    def test_database_error_is_not_a_success_response(self):
        with patch.dict(
            os.environ,
            {"DATABASE_URL": "postgresql://invalid:invalid@127.0.0.1:1/invalid"},
        ):
            self.assertEqual(self.client.get("/concerts").status_code, 503)

    def test_duplicate_merge_requires_matching_venue_and_aliases_can_be_corrected(self):
        first = concert(1101, "Концерт группы «Причастие»", 20, venue="Клуб Один")
        same = concert(1102, "Концерт группы «Причастие»", 20, venue="Клуб Один")
        different = concert(1103, "Концерт группы «Причастие»", 20, venue="Клуб Два")
        store_events(self.database_url, [first, same, different])
        kudago = normalize_kudago(
            {
                "id": 9001,
                "title": first.title,
                "location": {"slug": "msk"},
                "place": {"title": "Клуб Один"},
                "dates": [{"start": int(first.starts_at.timestamp())}],
                "site_url": "https://kudago.com/event/9001/",
            }
        )
        store_events(self.database_url, [kudago])
        store_events(self.database_url, [first, same, different])
        with psycopg.connect(self.database_url) as connection:
            rows = connection.execute(
                "SELECT external_id, concert_id FROM research_source_events WHERE external_id IN ('1101', '1102', '1103') ORDER BY external_id"
            ).fetchall()
            self.assertEqual(rows[0][1], rows[1][1])
            self.assertNotEqual(rows[0][1], rows[2][1])
            self.assertEqual(
                connection.execute(
                    "SELECT concert_id FROM research_source_events WHERE source = 'kudago' AND external_id = '9001'"
                ).fetchone()[0],
                rows[0][1],
            )
            self.assertGreaterEqual(
                connection.execute("SELECT count(*) FROM duplicate_reviews").fetchone()[
                    0
                ],
                1,
            )
            artist_id = connection.execute(
                "SELECT id FROM artists WHERE name = 'Причастие'"
            ).fetchone()[0]
            add_artist_alias(connection.cursor(), artist_id, "Prichastie")
            mention_id = connection.execute(
                "SELECT id FROM artist_mentions WHERE external_id = '1101' AND confidence = 'high' LIMIT 1"
            ).fetchone()[0]
            review_artist_mention(connection.cursor(), mention_id, None)
        store_events(self.database_url, [first])
        with psycopg.connect(self.database_url) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT artist_id FROM artist_mentions WHERE id = %s", (mention_id,)
                ).fetchone()[0]
            )
        self.assertEqual(
            self.client.get("/artists?q=Prichastie").json()["items"][0]["id"],
            artist_id,
        )

    def test_reclassification_hides_event_without_losing_concert_identity(self):
        original = concert(1201, "Концерт группы «Лето»", 12)
        store_events(self.database_url, [original])
        with psycopg.connect(self.database_url) as connection:
            concert_id = connection.execute(
                "SELECT concert_id FROM research_source_events WHERE external_id = '1201'"
            ).fetchone()[0]
        store_events(
            self.database_url, [concert(1201, "Стендап открытый микрофон", 12)]
        )
        self.assertEqual(self.client.get(f"/concerts/{concert_id}").status_code, 404)
        store_events(self.database_url, [original])
        self.assertEqual(self.client.get(f"/concerts/{concert_id}").status_code, 200)

    def test_web_login_reuses_mini_app_profile_and_protects_writes(self):
        signed = signed_data(
            444, "test-token", int(datetime.now(timezone.utc).timestamp())
        )
        mini_profile = self.client.post(
            "/auth/telegram", json={"init_data": signed}
        ).json()
        self.client.patch(
            "/me", headers={"X-Telegram-Init-Data": signed}, json={"city": "Москва"}
        )
        with (
            patch.dict(os.environ, OIDC_TEST_ENV),
            TestClient(app, base_url="https://concerts.test") as browser,
        ):
            config = browser.get("/auth/web/config").json()
            self.assertEqual(
                config["login_url"], "https://concerts.test/api/auth/web/start"
            )
            start = browser.get("/auth/web/start", follow_redirects=False)
            self.assertEqual(start.status_code, 303)
            self.assertIn("Secure", start.headers["set-cookie"])
            self.assertIn("HttpOnly", start.headers["set-cookie"])
            parameters = parse_qs(urlsplit(start.headers["location"]).query)
            state = parameters["state"][0]
            self.assertEqual(parameters["scope"], ["openid profile"])
            with (
                patch(
                    "app.web_routes.exchange_code", return_value=("id-token", "access")
                ) as exchange,
                patch(
                    "app.web_routes.verify_id_token",
                    return_value=WebIdentity(444, "Тестовый слушатель", "listener"),
                ),
            ):
                callback = browser.get(
                    f"/auth/web/callback?code=approved&state={state}",
                    follow_redirects=False,
                )
            self.assertEqual(callback.status_code, 303)
            self.assertEqual(callback.headers["location"], "https://concerts.test/")
            self.assertEqual(exchange.call_count, 1)
            web_session = browser.get("/auth/web/session").json()
            self.assertEqual(web_session["profile"]["id"], mini_profile["id"])
            self.assertEqual(web_session["profile"]["city"], "Москва")
            self.assertEqual(browser.get("/me").json()["id"], mini_profile["id"])
            self.assertEqual(
                browser.patch("/me", json={"city": "Санкт-Петербург"}).status_code,
                403,
            )
            self.assertEqual(
                browser.patch(
                    "/me",
                    json={"city": "Санкт-Петербург"},
                    headers={"X-CSRF-Token": web_session["csrf_token"]},
                ).status_code,
                200,
            )
            self.assertEqual(browser.post("/auth/web/logout").status_code, 403)
            self.assertEqual(
                browser.post(
                    "/auth/web/logout",
                    headers={"X-CSRF-Token": web_session["csrf_token"]},
                ).status_code,
                200,
            )
            self.assertEqual(browser.get("/auth/web/session").status_code, 401)
            browser.cookies.set("gigradar_login_state", state, domain="concerts.test")
            self.assertIn(
                "auth=failed",
                browser.get(
                    f"/auth/web/callback?code=approved&state={state}",
                    follow_redirects=False,
                ).headers["location"],
            )

    def test_web_login_rejects_missing_browser_state_and_expired_attempt(self):
        with (
            patch.dict(os.environ, OIDC_TEST_ENV),
            TestClient(app, base_url="https://concerts.test") as browser,
        ):
            start = browser.get("/auth/web/start", follow_redirects=False)
            state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
            rejected = browser.get(
                f"/auth/web/callback?code=approved&state={state}",
                headers={"Cookie": "gigradar_login_state=wrong"},
                follow_redirects=False,
            )
            self.assertIn("auth=failed", rejected.headers["location"])
            with psycopg.connect(self.database_url) as database:
                database.execute(
                    "UPDATE web_login_attempts SET expires_at = now() - interval '1 second' "
                    "WHERE state_hash = %s",
                    (token_hash(state),),
                )
            browser.cookies.set("gigradar_login_state", state, domain="concerts.test")
            with patch("app.web_routes.exchange_code") as exchange:
                expired = browser.get(
                    f"/auth/web/callback?code=approved&state={state}",
                    follow_redirects=False,
                )
            self.assertIn("auth=failed", expired.headers["location"])
            exchange.assert_not_called()

    def test_web_login_provider_failure_does_not_create_session(self):
        with (
            patch.dict(os.environ, OIDC_TEST_ENV),
            TestClient(app, base_url="https://concerts.test") as browser,
        ):
            for failure, outcome in (
                (InvalidIdentity("invalid"), "failed"),
                (ProviderUnavailable("offline"), "unavailable"),
            ):
                with self.subTest(outcome=outcome):
                    start = browser.get("/auth/web/start", follow_redirects=False)
                    state = parse_qs(urlsplit(start.headers["location"]).query)[
                        "state"
                    ][0]
                    with patch("app.web_routes.exchange_code", side_effect=failure):
                        callback = browser.get(
                            f"/auth/web/callback?code=approved&state={state}",
                            follow_redirects=False,
                        )
                    self.assertIn(f"auth={outcome}", callback.headers["location"])
                    self.assertEqual(browser.get("/auth/web/session").status_code, 401)

    def test_web_sessions_keep_users_separate(self):
        with (
            patch.dict(os.environ, OIDC_TEST_ENV),
            TestClient(app, base_url="https://concerts.test") as first_browser,
            TestClient(app, base_url="https://concerts.test") as second_browser,
        ):
            for browser, telegram_id in ((first_browser, 555), (second_browser, 666)):
                start = browser.get("/auth/web/start", follow_redirects=False)
                state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
                with (
                    patch(
                        "app.web_routes.exchange_code",
                        return_value=("id-token", "access"),
                    ),
                    patch(
                        "app.web_routes.verify_id_token",
                        return_value=WebIdentity(
                            telegram_id, f"User {telegram_id}", None
                        ),
                    ),
                ):
                    self.assertEqual(
                        browser.get(
                            f"/auth/web/callback?code=approved&state={state}",
                            follow_redirects=False,
                        ).status_code,
                        303,
                    )
            first_session = first_browser.get("/auth/web/session").json()
            second_session = second_browser.get("/auth/web/session").json()
            self.assertNotEqual(
                first_session["profile"]["id"], second_session["profile"]["id"]
            )
            concert_id = self.client.get("/concerts?city=Москва").json()["items"][0][
                "id"
            ]
            self.assertEqual(
                first_browser.put(
                    f"/me/favorites/{concert_id}",
                    headers={"X-CSRF-Token": first_session["csrf_token"]},
                ).status_code,
                200,
            )
            self.assertEqual(second_browser.get("/me/favorites").json(), [])
            self.assertEqual(
                second_browser.put(
                    f"/me/favorites/{concert_id}",
                    headers={"X-CSRF-Token": first_session["csrf_token"]},
                ).status_code,
                403,
            )


if __name__ == "__main__":
    unittest.main()
