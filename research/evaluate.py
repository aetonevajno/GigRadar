from __future__ import annotations

import csv
import json
import logging
from collections import Counter
from pathlib import Path

import psycopg
from config import database_url as configured_database_url
from curation import artist_mentions, classify
from normalize import normalize_timepad

ROOT = Path(__file__).resolve().parents[1]
CLASSIFICATION_LABELS = {
    "музыкальный концерт": "concert",
    "не концерт": "not_concert",
    "неясно": "unclear",
}
logger = logging.getLogger("gigradar.evaluate")


def classification_evaluation() -> tuple[Counter, list[tuple[str, str, str]]]:
    sample = json.loads(
        (ROOT / "research/output/timepad_sample_2026-10-09.json").read_text(
            encoding="utf-8"
        )
    )
    payloads = {
        str(record["id"]): {
            "id": record["id"],
            "name": record["name"],
            "description_short": record["description_short"],
            "starts_at": record["starts_at"],
            "location": {"city": record["city"]},
        }
        for city_records in sample.values()
        for record in city_records
    }
    confusion = Counter()
    errors = []
    with (ROOT / "docs/research/timepad-sample-2026-10-09.csv").open(
        encoding="utf-8-sig"
    ) as source:
        for label in csv.DictReader(source):
            predicted = classify(normalize_timepad(payloads[label["timepad_id"]])).kind
            expected = CLASSIFICATION_LABELS[label["итоговый_статус"]]
            confusion[(expected, predicted)] += 1
            if expected != predicted:
                errors.append((label["timepad_id"], expected, predicted))
    return confusion, errors


def artist_evaluation(
    database_url: str,
) -> tuple[int, int, int, list[tuple[str, str, str]]]:
    true_positive = false_positive = false_negative = 0
    errors = []
    with (
        (ROOT / "docs/research/artist-evaluation-2026-10-09.csv").open(
            encoding="utf-8"
        ) as source,
        psycopg.connect(database_url) as connection,
    ):
        for label in csv.DictReader(source, delimiter=";"):
            payload = connection.execute(
                "SELECT source_payload FROM research_source_events WHERE source = 'timepad' AND external_id = %s",
                (label["timepad_id"],),
            ).fetchone()[0]
            predicted = {
                mention.name.casefold()
                for mention in artist_mentions(normalize_timepad(payload))
                if mention.confidence == "high"
            }
            expected = {
                name.casefold() for name in label["expected_artists"].split("|") if name
            }
            true_positive += len(predicted & expected)
            false_positive += len(predicted - expected)
            false_negative += len(expected - predicted)
            if predicted != expected:
                errors.append(
                    (
                        label["timepad_id"],
                        "|".join(sorted(expected)),
                        "|".join(sorted(predicted)),
                    )
                )
    return true_positive, false_positive, false_negative, errors


def main() -> None:
    database_url = configured_database_url()
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    confusion, classification_errors = classification_evaluation()
    true_positive, false_positive, false_negative, artist_errors = artist_evaluation(
        database_url
    )
    logger.info("Classification confusion: %s", dict(confusion))
    logger.info("Classification errors: %s", classification_errors)
    logger.info(
        "Artist precision: %d/%d", true_positive, true_positive + false_positive
    )
    logger.info("Artist recall: %d/%d", true_positive, true_positive + false_negative)
    logger.info("Artist errors: %s", artist_errors)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    main()
