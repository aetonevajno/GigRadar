from __future__ import annotations

import argparse
import json
import logging
import sys
import time as clock
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import psycopg
from config import database_url as configured_database_url
from config import telegram_bot_token

MOSCOW_TIME = ZoneInfo("Europe/Moscow")
logger = logging.getLogger("gigradar.notify")


@dataclass(frozen=True)
class PendingMessage:
    notification_id: int
    telegram_id: int
    kind: str
    title: str
    starts_at: datetime
    city: str
    venue: str | None
    source_url: str | None


class DeliveryFailure(Exception):
    def __init__(self, message: str, state: str, retry_after: int = 0):
        super().__init__(message)
        self.state = state
        self.retry_after = retry_after


def retry_after_seconds(body: object) -> int:
    if isinstance(body, dict):
        parameters = body.get("parameters")
        if isinstance(parameters, dict):
            seconds = parameters.get("retry_after")
            if type(seconds) is int and seconds > 0:
                return seconds
    return 60


def claim_next(database_url: str) -> PendingMessage | None:
    while True:
        with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT n.id, u.telegram_id, n.kind, c.title, c.starts_at, c.city, c.venue, "
                "(SELECT source_url FROM research_source_events s WHERE s.concert_id = c.id "
                "AND s.classification = 'concert' AND source_url IS NOT NULL ORDER BY s.source LIMIT 1), "
                "u.notifications_enabled, c.status, "
                "EXISTS (SELECT 1 FROM user_favorites f WHERE f.user_id = u.id AND f.concert_id = c.id), "
                "EXISTS (SELECT 1 FROM user_artist_subscriptions s JOIN concert_artists ca "
                "ON ca.artist_id = s.artist_id WHERE s.user_id = u.id AND ca.concert_id = c.id), "
                "u.city, n.attempts, EXISTS (SELECT 1 FROM research_source_events active_source "
                "WHERE active_source.concert_id = c.id AND active_source.classification = 'concert') "
                "FROM notification_outbox n JOIN users u ON u.id = n.user_id "
                "JOIN concerts c ON c.id = n.concert_id "
                "WHERE n.delivery_state = 'queued' AND n.due_at <= now() "
                "ORDER BY n.due_at, n.id FOR UPDATE OF n SKIP LOCKED LIMIT 1"
            )
            row = cursor.fetchone()
            if row is None:
                return None
            (
                notification_id,
                telegram_id,
                kind,
                title,
                starts_at,
                city,
                venue,
                source_url,
                enabled,
                status,
                favorite,
                subscribed,
                user_city,
                attempts,
                active_concert,
            ) = row
            eligible = (
                enabled and active_concert and (user_city is None or user_city == city)
            )
            if kind == "new_concert":
                eligible = (
                    eligible
                    and subscribed
                    and status == "scheduled"
                    and starts_at > datetime.now(starts_at.tzinfo)
                )
            elif kind == "reminder":
                eligible = (
                    eligible
                    and favorite
                    and status != "cancelled"
                    and starts_at > datetime.now(starts_at.tzinfo)
                )
            else:
                eligible = eligible and (favorite or subscribed)
            if not eligible or attempts >= 5:
                reason = "No longer eligible" if not eligible else "Retry limit reached"
                cursor.execute(
                    "UPDATE notification_outbox SET delivery_state = 'failed', last_error = %s WHERE id = %s",
                    (reason, notification_id),
                )
                logger.log(
                    logging.WARNING if not eligible else logging.ERROR,
                    "Notification skipped id=%d: %s",
                    notification_id,
                    reason,
                )
                continue
            cursor.execute(
                "UPDATE notification_outbox SET delivery_state = 'sending', claimed_at = now(), attempts = attempts + 1 "
                "WHERE id = %s",
                (notification_id,),
            )
        return PendingMessage(
            notification_id,
            telegram_id,
            kind,
            title,
            starts_at,
            city,
            venue,
            source_url,
        )


def send_message(token: str, pending: PendingMessage) -> int:
    local_start = pending.starts_at.astimezone(MOSCOW_TIME).strftime("%d.%m.%Y в %H:%M")
    prefix = {
        "new_concert": "Новый концерт подписанного артиста",
        "reminder": "Напоминание о концерте",
        "rescheduled": "Концерт перенесён",
        "cancelled": "Концерт отменён",
    }[pending.kind]
    message = f"{prefix}\n{pending.title}\n{pending.city}, {local_start}"
    if pending.venue:
        message += f"\n{pending.venue}"
    payload: dict[str, object] = {"chat_id": pending.telegram_id, "text": message}
    if pending.source_url and pending.source_url.startswith("https://"):
        payload["reply_markup"] = {
            "inline_keyboard": [
                [{"text": "Открыть источник", "url": pending.source_url}]
            ]
        }
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            body = json.load(response)
    except HTTPError as exc:
        try:
            body = json.loads(exc.read(2048))
        except (ValueError, UnicodeDecodeError):
            body = {}
        finally:
            exc.close()
        if exc.code == 429:
            raise DeliveryFailure(
                "Telegram rate limit", "queued", retry_after_seconds(body)
            ) from exc
        state = "uncertain" if exc.code >= 500 else "failed"
        raise DeliveryFailure(f"Telegram HTTP {exc.code}", state) from exc
    except (URLError, TimeoutError) as exc:
        raise DeliveryFailure(
            "Telegram response was not received", "uncertain"
        ) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DeliveryFailure("Telegram returned invalid JSON", "uncertain") from exc
    if not isinstance(body, dict) or body.get("ok") is not True:
        code = body.get("error_code") if isinstance(body, dict) else None
        if code == 429:
            raise DeliveryFailure(
                "Telegram rate limit", "queued", retry_after_seconds(body)
            )
        raise DeliveryFailure(f"Telegram rejected message: {code}", "failed")
    try:
        return int(body["result"]["message_id"])
    except (KeyError, ValueError, TypeError) as exc:
        raise DeliveryFailure(
            "Telegram response has no message ID", "uncertain"
        ) from exc


def deliver_once(database_url: str, token: str, *, limit: int = 20) -> int:
    delivered = 0
    for _ in range(limit):
        pending = claim_next(database_url)
        if pending is None:
            break
        try:
            message_id = send_message(token, pending)
        except DeliveryFailure as exc:
            with psycopg.connect(database_url) as connection:
                connection.execute(
                    "UPDATE notification_outbox SET delivery_state = %s, due_at = %s, last_error = %s "
                    "WHERE id = %s AND delivery_state = 'sending'",
                    (
                        exc.state,
                        datetime.now(MOSCOW_TIME) + timedelta(seconds=exc.retry_after),
                        str(exc),
                        pending.notification_id,
                    ),
                )
            logger.log(
                logging.WARNING if exc.state == "queued" else logging.ERROR,
                "Notification delivery id=%d state=%s: %s",
                pending.notification_id,
                exc.state,
                exc,
            )
        else:
            with psycopg.connect(database_url) as connection:
                connection.execute(
                    "UPDATE notification_outbox SET delivery_state = 'sent', sent_at = now(), "
                    "telegram_message_id = %s WHERE id = %s AND delivery_state = 'sending'",
                    (message_id, pending.notification_id),
                )
            delivered += 1
        clock.sleep(0.05)
    return delivered


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deliver GigRadar Telegram notifications"
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    database_url = configured_database_url()
    token = telegram_bot_token()
    if not database_url or not token:
        parser.error("DATABASE_URL and TELEGRAM_BOT_TOKEN are required")
    try:
        while True:
            delivered = deliver_once(database_url, token)
            if delivered:
                logger.info("Telegram notifications delivered count=%d", delivered)
            if args.once:
                return 0
            clock.sleep(5)
    except KeyboardInterrupt:
        return 0
    except psycopg.Error:
        logger.exception("Notification database error")
        return 1


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    sys.exit(main())
