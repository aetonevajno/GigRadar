import unittest

from curation import artist_mentions, assess_duplicate, classify
from normalize import normalize_timepad


def event(
    event_id, title, *, description="", start="2026-10-20T19:00:00+03:00", venue="Клуб"
):
    return normalize_timepad(
        {
            "id": event_id,
            "name": title,
            "description_short": description,
            "starts_at": start,
            "location": {"city": "Москва", "address": venue},
        }
    )


class CurationTests(unittest.TestCase):
    def test_obvious_nonconcert_and_ambiguous_events_stay_out_of_feed(self):
        self.assertEqual(
            classify(event(1, "Стендап открытый микрофон")).kind, "not_concert"
        )
        self.assertEqual(classify(event(2, "Концерт и квиз")).kind, "unclear")
        self.assertEqual(
            classify(event(3, "Абонемент на три концерта")).kind, "unclear"
        )
        self.assertEqual(
            classify(event(4, "Камерный концерт группы «Лето»")).kind, "concert"
        )

    def test_artist_provenance_and_historical_reference(self):
        performance = event(1, "Концерт группы «Причастие»")
        self.assertEqual(
            [
                (mention.name, mention.field, mention.confidence)
                for mention in artist_mentions(performance)
            ],
            [("Причастие", "title", "high")],
        )
        reference = event(
            2,
            "Камерный концерт Владимира Полякова",
            description="Продюсер первых альбомов группы «Манго-Манго». Живой звук.",
        )
        self.assertFalse(
            any(
                mention.name == "Манго-Манго" and mention.confidence == "high"
                for mention in artist_mentions(reference)
            )
        )

    def test_duplicate_requires_time_venue_title_without_conflicting_artists(self):
        first = event(1, "Концерт группы «Причастие»")
        same = event(2, "Концерт группы «Причастие»")
        other_venue = event(3, "Концерт группы «Причастие»", venue="Другой клуб")
        other_time = event(
            4, "Концерт группы «Причастие»", start="2026-10-20T22:00:00+03:00"
        )
        self.assertEqual(
            assess_duplicate(first, same, {"причастие"}, {"причастие"}).decision,
            "merge",
        )
        self.assertEqual(
            assess_duplicate(first, other_venue, set(), set()).decision, "review"
        )
        self.assertEqual(
            assess_duplicate(first, other_time, set(), set()).decision, "distinct"
        )
        self.assertEqual(
            assess_duplicate(first, same, {"а"}, {"б"}).decision, "distinct"
        )


if __name__ == "__main__":
    unittest.main()
