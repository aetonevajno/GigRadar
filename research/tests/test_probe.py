import io
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch
from urllib.error import HTTPError

from normalize import (
    duplicate_candidates,
    normalize_kudago,
    normalize_timepad,
    quality_metrics,
)
from probe import (
    ProbeError,
    collect_kudago,
    collect_timepad,
    fetch_json,
    timepad_music_categories,
)


class NormalizationTests(unittest.TestCase):
    def test_kudago_selects_occurrence_inside_window_and_music_participant(self):
        old = datetime(2026, 10, 1, 17, tzinfo=timezone.utc)
        current = datetime(2026, 10, 15, 17, tzinfo=timezone.utc)
        event = normalize_kudago(
            {
                "id": 12,
                "title": "Концерт группы",
                "location": {"slug": "msk"},
                "place": {"title": "Клуб"},
                "dates": [
                    {"start": int(old.timestamp())},
                    {"start": int(current.timestamp())},
                ],
                "participants": [
                    {"role": {"slug": "musician"}, "agent": {"title": "Группа"}},
                    {"role": {"slug": "stage_theatre"}, "agent": {"title": "Театр"}},
                ],
            },
            date(2026, 10, 9),
            date(2026, 11, 8),
        )
        self.assertEqual(event.city, "Москва")
        self.assertEqual(event.starts_at.isoformat(), "2026-10-15T20:00:00+03:00")
        self.assertEqual(event.artist_candidates, ("Группа",))

    def test_timepad_requires_timezone_for_complete_date(self):
        event = normalize_timepad(
            {
                "id": 21,
                "name": "Выступление",
                "starts_at": "2026-10-20T19:00:00",
                "location": {"city": "Санкт-Петербург"},
            }
        )
        self.assertIsNone(event.starts_at)
        self.assertEqual(quality_metrics([event])["city_and_date_present_percent"], 0.0)

    def test_duplicate_candidates_require_cross_source_city_date_and_title(self):
        start = datetime(2026, 10, 15, 17, tzinfo=timezone.utc)
        kudago = normalize_kudago(
            {
                "id": 12,
                "title": "Концерт: Группа",
                "location": {"slug": "msk"},
                "dates": [{"start": int(start.timestamp())}],
            }
        )
        timepad = normalize_timepad(
            {
                "id": 21,
                "name": "Концерт — группа",
                "starts_at": "2026-10-15T20:00:00+03:00",
                "location": {"city": "Москва"},
            }
        )
        self.assertEqual(len(duplicate_candidates([kudago, timepad])), 1)
        self.assertIsNone(
            quality_metrics([kudago, timepad])["artist_identification_accuracy_percent"]
        )


class CollectionTests(unittest.TestCase):
    def test_kudago_rejects_wrong_city_without_claiming_a_valid_event(self):
        payload = {
            "results": [{"id": 1, "title": "Шоу", "location": {"slug": "spb"}}],
            "next": None,
        }
        with patch("probe.fetch_json", return_value=payload):
            events, rejected = collect_kudago(
                "msk", date(2026, 10, 9), date(2026, 11, 8), 10
            )
        self.assertEqual(events, [])
        self.assertEqual(rejected, 1)

    def test_http_error_surfaces_provider_message(self):
        error = HTTPError(
            "https://api.timepad.ru/v1/events.json",
            403,
            "Forbidden",
            {},
            io.BytesIO(b'{"response_status":{"message":"Token required"}}'),
        )
        with (
            patch("probe.urlopen", side_effect=error),
            self.assertRaisesRegex(ProbeError, "HTTP 403: Token required"),
        ):
            fetch_json("https://api.timepad.ru/v1/events.json", {})

    def test_timepad_category_and_event_mapping(self):
        categories = {"values": [{"id": 7, "name": "Музыка и концерты"}]}
        with patch("probe.fetch_json", return_value=categories):
            self.assertEqual(timepad_music_categories("token"), [7])
        response = {
            "total": "1",
            "values": [
                {
                    "id": 55,
                    "name": "Концерт группы",
                    "starts_at": "2026-10-20T19:00:00+03:00",
                    "location": {"city": "Москва", "address": "Клуб"},
                    "url": "https://example.org/event/55",
                }
            ],
        }
        with patch("probe.fetch_json", return_value=response):
            events, rejected = collect_timepad(
                "Москва", date(2026, 10, 9), date(2026, 11, 8), 10, [7], "token"
            )
        self.assertEqual(rejected, 0)
        self.assertEqual(events[0].starts_at.isoformat(), "2026-10-20T19:00:00+03:00")


if __name__ == "__main__":
    unittest.main()
