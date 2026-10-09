from __future__ import annotations

import argparse
import json
import os
import sys
import time as clock
from datetime import date, datetime, time, timedelta

from normalize import CITY_NAMES
from probe import MOSCOW_TIME, ProbeError, TimepadFetchOptions, collect_timepad
from store import StorageError, list_catalog_events, sync_timepad_city

TIMEPAD_CONCERT_CATEGORY_ID = 460


def sync_once(args: argparse.Namespace, database_url: str, token: str) -> int:
    since = getattr(args, "since", None) or datetime.now(MOSCOW_TIME).date()
    until = since + timedelta(days=args.days)
    cities = [args.city] if args.city else list(CITY_NAMES)
    outcomes = []
    for slug in cities:
        city = CITY_NAMES[slug]
        try:
            outcome = sync_timepad_city(
                database_url,
                city,
                since,
                until,
                timedelta(hours=args.refresh_hours),
                lambda city=city: collect_timepad(
                    city,
                    since,
                    until,
                    TimepadFetchOptions(
                        None, fields="location,categories", page_size=10
                    ),
                    [TIMEPAD_CONCERT_CATEGORY_ID],
                    token,
                ),
                force=getattr(args, "force", False),
            )
        except (ProbeError, StorageError) as exc:
            outcomes.append({"city": city, "status": "error", "error": str(exc)})
            continue
        outcomes.append(
            {
                "city": city,
                "status": outcome.status,
                "events": outcome.event_count,
                "rejected": outcome.rejected_count,
            }
        )
    print(json.dumps(outcomes, ensure_ascii=False, indent=2), flush=True)
    return 1 if any(outcome["status"] == "error" for outcome in outcomes) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Local GigRadar event catalog")
    commands = parser.add_subparsers(dest="command", required=True)

    sync_parser = commands.add_parser(
        "sync", help="Refresh Timepad events in PostgreSQL"
    )
    sync_parser.add_argument("--since", type=date.fromisoformat)
    sync_parser.add_argument("--days", type=int, default=30)
    sync_parser.add_argument("--city", choices=CITY_NAMES)
    sync_parser.add_argument("--refresh-hours", type=float, default=6)
    sync_parser.add_argument("--force", action="store_true")

    watch_parser = commands.add_parser("watch", help="Refresh Timepad on a schedule")
    watch_parser.add_argument("--days", type=int, default=30)
    watch_parser.add_argument("--city", choices=CITY_NAMES)
    watch_parser.add_argument("--refresh-hours", type=float, default=6)
    watch_parser.add_argument("--interval-minutes", type=int, default=60)

    list_parser = commands.add_parser("list", help="Read events from PostgreSQL")
    list_parser.add_argument("--since", type=date.fromisoformat)
    list_parser.add_argument("--days", type=int, default=30)
    list_parser.add_argument("--city", choices=CITY_NAMES)
    list_parser.add_argument("--limit", type=int, default=100)
    list_parser.add_argument("--offset", type=int, default=0)

    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be positive")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is required")

    if args.command == "list":
        if not 1 <= args.limit <= 1000 or args.offset < 0:
            parser.error("--limit must be 1..1000 and --offset must be nonnegative")
        since = (
            datetime.combine(args.since, time.min, MOSCOW_TIME)
            if args.since
            else datetime.now(MOSCOW_TIME)
        )
        try:
            events = list_catalog_events(
                database_url,
                since,
                since + timedelta(days=args.days),
                CITY_NAMES.get(args.city),
                args.limit,
                args.offset,
            )
        except StorageError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps(events, ensure_ascii=False, indent=2))
        return 0

    if args.refresh_hours <= 0:
        parser.error("--refresh-hours must be positive")
    if args.command == "watch" and args.interval_minutes < 1:
        parser.error("--interval-minutes must be positive")
    token = os.environ.get("TIMEPAD_API_TOKEN")
    if not token:
        parser.error("TIMEPAD_API_TOKEN is required for sync")
    if args.command == "sync":
        return sync_once(args, database_url, token)
    try:
        while True:
            sync_once(args, database_url, token)
            clock.sleep(args.interval_minutes * 60)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
