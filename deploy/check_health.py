from __future__ import annotations

import logging
import os
import sys
from datetime import timedelta

import psycopg

logger = logging.getLogger("gigradar.health")


def main() -> int:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL is required")
        return 2
    try:
        with psycopg.connect(database_url) as connection:
            sync_rows = connection.execute(
                "SELECT city, last_successful_at, now() - last_successful_at "
                "FROM research_sync_state WHERE source = 'timepad'"
            ).fetchall()
            problem_rows = connection.execute(
                "SELECT delivery_state, count(*) FROM notification_outbox "
                "WHERE delivery_state IN ('failed', 'uncertain') "
                "GROUP BY delivery_state"
            ).fetchall()
            stalled = connection.execute(
                "SELECT count(*) FROM notification_outbox WHERE "
                "(delivery_state = 'sending' AND claimed_at < now() - interval '5 minutes') "
                "OR (delivery_state = 'queued' AND due_at < now() - interval '30 minutes')"
            ).fetchone()[0]
    except psycopg.Error:
        logger.exception("Database health check failed")
        return 2
    if len(sync_rows) < 2 or any(age > timedelta(hours=12) for _, _, age in sync_rows):
        logger.error("Timepad import is stale: %s", sync_rows)
        return 1
    if problem_rows:
        logger.error("Notification delivery needs attention: %s", problem_rows)
        return 1
    if stalled:
        logger.error("Notification delivery is stalled: %d", stalled)
        return 1
    logger.info("Import and notification delivery are healthy")
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    sys.exit(main())
