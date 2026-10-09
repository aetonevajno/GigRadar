from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time as clock
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from normalize import (
    CITY_NAMES,
    Event,
    normalize_kudago,
    normalize_timepad,
    quality_metrics,
)
from store import StorageError, store_events

KUDAGO_URL = "https://kudago.com/public-api/v1.4/events/"
TIMEPAD_URL = "https://api.timepad.ru/v1/events.json"
TIMEPAD_CATEGORIES_URL = "https://api.timepad.ru/v1/dictionary/event_categories.json"
MOSCOW_TIME = ZoneInfo("Europe/Moscow")
logger = logging.getLogger("gigradar.probe")


class ProbeError(Exception):
    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True)
class TimepadFetchOptions:
    limit: int | None
    fields: str = "location,categories,description_short"
    page_size: int = 100
    min_interval_seconds: float = 0.0


_last_timepad_request_at = 0.0


def fetch_json(url: str, params: dict[str, object], token: str | None = None) -> dict:
    query = urlencode(params)
    headers = {
        "Accept": "application/json",
        "User-Agent": "GigRadar-source-research/0.1",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(f"{url}?{query}" if query else url, headers=headers)
    try:
        with urlopen(request, timeout=20) as response:
            result = json.load(response)
    except HTTPError as exc:
        try:
            body = json.loads(exc.read(1024))
            status = body.get("response_status") if isinstance(body, dict) else None
            detail = (
                status.get("message", exc.reason)
                if isinstance(status, dict)
                else exc.reason
            )
        except (json.JSONDecodeError, AttributeError, TypeError):
            detail = exc.reason
        finally:
            exc.close()
        raise ProbeError(
            f"HTTP {exc.code}: {detail}",
            transient=exc.code == 429 or 500 <= exc.code < 600,
        ) from exc
    except (URLError, TimeoutError) as exc:
        raise ProbeError(str(exc), transient=True) from exc
    except json.JSONDecodeError as exc:
        raise ProbeError("API returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise ProbeError("API returned a non-object response")
    return result


def collect_kudago(
    city_slug: str, since: date, until: date, limit: int
) -> tuple[list[Event], int]:
    start = datetime.combine(since, time.min, MOSCOW_TIME).astimezone(timezone.utc)
    end = datetime.combine(until, time.min, MOSCOW_TIME).astimezone(timezone.utc)
    events: list[Event] = []
    rejected = 0
    page = 1
    while len(events) + rejected < limit:
        size = min(100, limit - len(events) - rejected)
        payload = fetch_json(
            KUDAGO_URL,
            {
                "location": city_slug,
                "categories": "concert",
                "actual_since": start.isoformat(),
                "actual_until": end.isoformat(),
                "page": page,
                "page_size": size,
                "fields": "id,title,dates,place,location,participants,site_url",
                "expand": "place,location,dates,participants",
            },
        )
        rows = payload.get("results")
        if not isinstance(rows, list):
            raise ProbeError("KudaGo response has no results list")
        for row in rows:
            if not isinstance(row, dict):
                rejected += 1
                continue
            try:
                event = normalize_kudago(row, since, until)
            except (KeyError, TypeError, ValueError):
                rejected += 1
                continue
            if event.city != CITY_NAMES[city_slug] or not event.title:
                rejected += 1
                continue
            events.append(event)
        if not rows or not payload.get("next"):
            break
        page += 1
    return events, rejected


def timepad_music_categories(token: str | None) -> list[int]:
    payload = fetch_json(TIMEPAD_CATEGORIES_URL, {}, token)
    rows = payload.get("values")
    if not isinstance(rows, list):
        raise ProbeError("Timepad category response has no values list")
    ids = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        category_name = row.get("name")
        category_tag = row.get("tag")
        name = category_name if isinstance(category_name, str) else category_tag
        if not isinstance(name, str):
            continue
        name = name.casefold()
        if any(word in name for word in ("музык", "концерт", "music", "concert")):
            category_id = row.get("id")
            if isinstance(category_id, int):
                ids.append(category_id)
    if not ids:
        raise ProbeError(
            "Timepad music category was not found; pass --timepad-category-id"
        )
    return ids


def collect_timepad(
    city: str,
    since: date,
    until: date,
    options: TimepadFetchOptions,
    categories: list[int],
    token: str | None,
) -> tuple[list[Event], int]:
    if not 1 <= options.page_size <= 100:
        raise ValueError("Timepad page size must be between 1 and 100")
    events: list[Event] = []
    rejected = 0
    skip = 0
    expected_total: int | None = None
    global _last_timepad_request_at
    while options.limit is None or len(events) + rejected < options.limit:
        size = (
            options.page_size
            if options.limit is None
            else min(options.page_size, options.limit - len(events) - rejected)
        )
        parameters = {
            "cities": city,
            "starts_at_min": datetime.combine(since, time.min, MOSCOW_TIME).isoformat(),
            "starts_at_max": datetime.combine(until, time.min, MOSCOW_TIME).isoformat(),
            "category_ids": ",".join(map(str, categories)),
            "fields": options.fields,
            "sort": "+starts_at",
            "limit": size,
            "skip": skip,
        }
        for attempt in range(3):
            remaining = options.min_interval_seconds - (
                clock.monotonic() - _last_timepad_request_at
            )
            if remaining > 0:
                clock.sleep(remaining)
            _last_timepad_request_at = clock.monotonic()
            try:
                payload = fetch_json(TIMEPAD_URL, parameters, token)
                break
            except ProbeError as exc:
                if not exc.transient or attempt == 2:
                    raise ProbeError(
                        f"Timepad {city} page skip={skip} size={size}: {exc}",
                        transient=exc.transient,
                    ) from exc
                clock.sleep(2**attempt)
        rows = payload.get("values")
        if not isinstance(rows, list):
            raise ProbeError("Timepad response has no values list")
        total = payload.get("total")
        if not isinstance(total, (int, str)) or not str(total).isdigit():
            raise ProbeError("Timepad response has no valid total count")
        if expected_total is None:
            expected_total = int(total)
        elif int(total) != expected_total:
            raise ProbeError("Timepad total changed during pagination")
        if not rows and skip < expected_total:
            raise ProbeError("Timepad returned an incomplete event page")
        for row in rows:
            if not isinstance(row, dict):
                rejected += 1
                continue
            try:
                event = normalize_timepad(row)
            except (KeyError, TypeError, ValueError):
                rejected += 1
                continue
            if event.city != city or not event.title:
                rejected += 1
                continue
            events.append(event)
        skip += len(rows)
        if skip >= expected_total:
            break
    return events, rejected


def run_probe(args: argparse.Namespace) -> tuple[dict, list[Event]]:
    until = args.since + timedelta(days=args.days)
    report: dict = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "window_start": args.since.isoformat(),
        "window_end_exclusive": until.isoformat(),
        "max_events_per_source_city": args.max_per_city,
        "attempts": [],
    }
    events: list[Event] = []
    for slug, city in CITY_NAMES.items():
        try:
            found, rejected = collect_kudago(slug, args.since, until, args.max_per_city)
            events.extend(found)
            report["attempts"].append(
                {
                    "source": "kudago",
                    "city": city,
                    "status": "ok",
                    "events": len(found),
                    "rejected": rejected,
                }
            )
        except ProbeError as exc:
            report["attempts"].append(
                {"source": "kudago", "city": city, "status": "error", "error": str(exc)}
            )

    token = os.environ.get("TIMEPAD_API_TOKEN")
    try:
        categories = args.timepad_category_id or timepad_music_categories(token)
    except ProbeError as exc:
        report["attempts"].append(
            {"source": "timepad", "city": "both", "status": "error", "error": str(exc)}
        )
    else:
        for city in CITY_NAMES.values():
            try:
                found, rejected = collect_timepad(
                    city,
                    args.since,
                    until,
                    TimepadFetchOptions(args.max_per_city),
                    categories,
                    token,
                )
                events.extend(found)
                report["attempts"].append(
                    {
                        "source": "timepad",
                        "city": city,
                        "status": "ok",
                        "events": len(found),
                        "rejected": rejected,
                    }
                )
            except ProbeError as exc:
                report["attempts"].append(
                    {
                        "source": "timepad",
                        "city": city,
                        "status": "error",
                        "error": str(exc),
                    }
                )
    unique_events = {(event.source, event.external_id): event for event in events}
    report["repeated_source_ids"] = len(events) - len(unique_events)
    deduplicated = list(unique_events.values())
    report["metrics"] = quality_metrics(deduplicated)
    return report, deduplicated


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe KudaGo and Timepad concert data"
    )
    parser.add_argument(
        "--since", type=date.fromisoformat, default=datetime.now(MOSCOW_TIME).date()
    )
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--max-per-city", type=int, default=100)
    parser.add_argument("--timepad-category-id", type=int, action="append")
    parser.add_argument(
        "--report", type=Path, default=Path("research/output/latest.json")
    )
    args = parser.parse_args()
    if args.days < 1 or args.max_per_city < 1:
        parser.error("--days and --max-per-city must be positive")
    report, events = run_probe(args)
    database_url = os.environ.get("DATABASE_URL")
    if database_url and events:
        try:
            report["stored_records"] = store_events(database_url, events)
        except StorageError as exc:
            report["storage_error"] = str(exc)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    logger.info("Source probe report saved path=%s", args.report)
    for attempt in report["attempts"]:
        if attempt["status"] == "error":
            logger.error(
                "Source probe failed source=%s city=%s: %s",
                attempt["source"],
                attempt["city"],
                attempt["error"],
            )
        else:
            logger.info(
                "Source probe source=%s city=%s events=%d rejected=%d",
                attempt["source"],
                attempt["city"],
                attempt["events"],
                attempt["rejected"],
            )
    if "storage_error" in report:
        logger.error("Source probe storage failed: %s", report["storage_error"])
    return (
        1
        if any(item["status"] == "error" for item in report["attempts"])
        or "storage_error" in report
        else 0
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    sys.exit(main())
