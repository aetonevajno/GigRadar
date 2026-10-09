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
    TimepadFetchOptions,
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

    def test_duplicate_candidates_match_cross_source_city_date_and_title(self):
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

    def test_duplicate_candidates_include_same_source_only_at_same_start(self):
        def timepad_event(event_id, title, starts_at):
            return normalize_timepad(
                {
                    "id": event_id,
                    "name": title,
                    "starts_at": starts_at,
                    "location": {"city": "Москва"},
                }
            )

        first = timepad_event(
            21, "Концерт &quot;Группа&quot;", "2026-10-15T20:00:00+03:00"
        )
        same_show = timepad_event(22, "Концерт «Группа»", "2026-10-15T20:00:00+03:00")
        later_show = timepad_event(23, "Концерт «Группа»", "2026-10-15T22:00:00+03:00")

        self.assertEqual(
            duplicate_candidates([first, same_show, later_show]),
            [{"first": "timepad:21", "second": "timepad:22"}],
        )
        self.assertEqual(duplicate_candidates([first, first]), [])


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
        response_body = io.BytesIO(b'{"response_status":{"message":"Token required"}}')
        error = HTTPError(
            "https://api.timepad.ru/v1/events.json",
            403,
            "Forbidden",
            {},
            response_body,
        )
        with (
            patch("probe.urlopen", side_effect=error),
            self.assertRaisesRegex(ProbeError, "HTTP 403: Token required"),
        ):
            fetch_json("https://api.timepad.ru/v1/events.json", {})
        self.assertTrue(response_body.closed)

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
                    "description_short": "Музыкальный концерт",
                    "url": "https://example.org/event/55",
                }
            ],
        }
        with patch("probe.fetch_json", return_value=response) as fetch_mock:
            events, rejected = collect_timepad(
                "Москва",
                date(2026, 10, 9),
                date(2026, 11, 8),
                TimepadFetchOptions(10),
                [7],
                "token",
            )
        self.assertEqual(rejected, 0)
        self.assertEqual(events[0].starts_at.isoformat(), "2026-10-20T19:00:00+03:00")
        self.assertEqual(events[0].payload["description_short"], "Музыкальный концерт")
        self.assertEqual(
            fetch_mock.call_args.args[1]["fields"],
            "location,categories,description_short",
        )

    def test_timepad_collects_short_descriptions_across_pages(self):
        events = [
            {
                "id": event_id,
                "name": f"Концерт {event_id}",
                "starts_at": "2026-10-20T19:00:00+03:00",
                "location": {"city": "Москва"},
                "description_short": "Музыка",
            }
            for event_id in range(101)
        ]
        responses = [
            {"total": 101, "values": events[:100]},
            {"total": 101, "values": events[100:]},
        ]
        with patch("probe.fetch_json", side_effect=responses) as fetch_mock:
            collected, rejected = collect_timepad(
                "Москва",
                date(2026, 10, 9),
                date(2026, 11, 8),
                TimepadFetchOptions(101),
                [460],
                "token",
            )

        self.assertEqual(len(collected), 101)
        self.assertEqual(rejected, 0)
        self.assertEqual(
            [call.args[1]["limit"] for call in fetch_mock.call_args_list], [100, 1]
        )
        self.assertEqual(
            [call.args[1]["skip"] for call in fetch_mock.call_args_list], [0, 100]
        )

    def test_timepad_collects_entire_window_without_limit(self):
        rows = [
            {
                "id": event_id,
                "name": f"Концерт {event_id}",
                "starts_at": "2026-10-20T19:00:00+03:00",
                "location": {"city": "Москва"},
            }
            for event_id in range(101)
        ]
        responses = [
            {"total": 101, "values": rows[:100]},
            {"total": 101, "values": rows[100:]},
        ]
        with patch("probe.fetch_json", side_effect=responses) as fetch_mock:
            collected, rejected = collect_timepad(
                "Москва",
                date(2026, 10, 9),
                date(2026, 11, 8),
                TimepadFetchOptions(None),
                [460],
                "token",
            )

        self.assertEqual((len(collected), rejected), (101, 0))
        self.assertEqual(
            [call.args[1]["limit"] for call in fetch_mock.call_args_list],
            [100, 100],
        )

    def test_timepad_fails_if_pagination_stops_early(self):
        responses = [
            {
                "total": 2,
                "values": [
                    {
                        "id": 1,
                        "name": "Концерт",
                        "starts_at": "2026-10-20T19:00:00+03:00",
                        "location": {"city": "Москва"},
                    }
                ],
            },
            {"total": 2, "values": []},
        ]
        with (
            patch("probe.fetch_json", side_effect=responses),
            self.assertRaisesRegex(ProbeError, "incomplete event page"),
        ):
            collect_timepad(
                "Москва",
                date(2026, 10, 9),
                date(2026, 11, 8),
                TimepadFetchOptions(None),
                [460],
                "token",
            )

    def test_timepad_uses_small_pages_and_basic_fields_for_catalog(self):
        rows = [
            {
                "id": event_id,
                "name": f"Концерт {event_id}",
                "starts_at": "2026-10-20T19:00:00+03:00",
                "location": {"city": "Санкт-Петербург"},
            }
            for event_id in range(11)
        ]
        responses = [
            {"total": 11, "values": rows[:10]},
            {"total": 11, "values": rows[10:]},
        ]
        with patch("probe.fetch_json", side_effect=responses) as fetch_mock:
            collected, rejected = collect_timepad(
                "Санкт-Петербург",
                date(2026, 10, 9),
                date(2026, 11, 8),
                TimepadFetchOptions(None, fields="location,categories", page_size=10),
                [460],
                "token",
            )

        self.assertEqual((len(collected), rejected), (11, 0))
        self.assertEqual(
            [call.args[1]["limit"] for call in fetch_mock.call_args_list],
            [10, 10],
        )
        self.assertEqual(
            [call.args[1]["fields"] for call in fetch_mock.call_args_list],
            ["location,categories", "location,categories"],
        )


if __name__ == "__main__":
    unittest.main()
