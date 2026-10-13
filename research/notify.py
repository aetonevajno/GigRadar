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

from app.telemetry import (
    NOTIFICATION_DELAY,
    NOTIFICATION_DELIVERIES,
    TELEGRAM_ERRORS,
    start_metrics_server,
)
from locales import translations_for

MOSCOW_TIME = ZoneInfo("Europe/Moscow")
logger = logging.getLogger("gigradar.notify")
CLAIM_LEASE = timedelta(minutes=1)


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
    locale: str


class DeliveryFailure(Exception):
    def __init__(
        self, message: str, state: str, retry_after: int = 0, outcome: str = "other"
    ):
        super().__init__(message)
        self.state = state
        self.retry_after = retry_after
        self.outcome = outcome


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
                "UPDATE notification_outbox SET delivery_state = 'uncertain', "
                "failure_reason = 'telegram', last_error = 'Delivery claim expired without a recorded result' "
                "WHERE delivery_state = 'sending' "
                "AND (claimed_at IS NULL OR claimed_at < now() - %s)",
                (CLAIM_LEASE,),
            )
            cursor.execute(
                "SELECT n.id, u.telegram_id, n.kind, c.title, c.starts_at, c.city, c.venue, "
                "(SELECT source_url FROM research_source_events s WHERE s.concert_id = c.id "
                "AND s.classification = 'concert' AND s.is_active AND source_url IS NOT NULL ORDER BY s.source LIMIT 1), "
                "u.notifications_enabled, c.status, "
                "EXISTS (SELECT 1 FROM user_favorites f WHERE f.user_id = u.id AND f.concert_id = c.id), "
                "EXISTS (SELECT 1 FROM user_artist_subscriptions s JOIN concert_artists ca "
                "ON ca.artist_id = s.artist_id WHERE s.user_id = u.id AND ca.concert_id = c.id), "
                "u.city, n.attempts, u.locale, EXISTS (SELECT 1 FROM research_source_events active_source "
                "WHERE active_source.concert_id = c.id AND active_source.classification = 'concert' AND active_source.is_active), "
                "u.bot_chat_available, n.change_id = (SELECT max(ch.id) FROM concert_changes ch "
                "WHERE ch.concert_id = c.id) "
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
                locale,
                active_concert,
                bot_chat_available,
                current_change,
            ) = row
            eligible = (
                enabled
                and bot_chat_available
                and active_concert
                and (user_city is None or user_city == city)
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
                eligible = eligible and (favorite or subscribed) and current_change
                if kind == "cancelled":
                    eligible = eligible and status == "cancelled"
                elif kind == "restored":
                    eligible = eligible and status == "scheduled"
                elif kind == "rescheduled":
                    eligible = eligible and status == "postponed"
            if not eligible or attempts >= 5:
                reason = "No longer eligible" if not eligible else "Retry limit reached"
                cursor.execute(
                    "UPDATE notification_outbox SET delivery_state = 'failed', failure_reason = %s, last_error = %s WHERE id = %s",
                    (
                        "ineligible" if not eligible else "retry_limit",
                        reason,
                        notification_id,
                    ),
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
            locale,
        )


def send_message(token: str, pending: PendingMessage) -> int:
    messages = translations_for(pending.locale)["notification"]
    starts_at = pending.starts_at.astimezone(MOSCOW_TIME)
    prefix = messages[pending.kind]
    local_start = f"{starts_at:%d.%m.%Y} {messages['date_separator']} {starts_at:%H:%M}"
    message = f"{prefix}\n{pending.title}\n{pending.city}, {local_start}"
    if pending.venue:
        message += f"\n{pending.venue}"
    payload: dict[str, object] = {"chat_id": pending.telegram_id, "text": message}
    if pending.source_url and pending.source_url.startswith("https://"):
        payload["reply_markup"] = {
            "inline_keyboard": [
                [{"text": messages["open_source"], "url": pending.source_url}]
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
        except ValueError, UnicodeDecodeError:
            body = {}
        finally:
            exc.close()
        if exc.code == 429:
            raise DeliveryFailure(
                "Telegram rate limit", "queued", retry_after_seconds(body), "http_429"
            ) from exc
        state = "uncertain" if exc.code >= 500 else "failed"
        raise DeliveryFailure(
            f"Telegram HTTP {exc.code}",
            state,
            outcome="forbidden" if exc.code == 403 else f"http_{exc.code // 100}xx",
        ) from exc
    except (URLError, TimeoutError) as exc:
        raise DeliveryFailure(
            "Telegram response was not received", "uncertain", outcome="network"
        ) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DeliveryFailure(
            "Telegram returned invalid JSON", "uncertain", outcome="invalid_response"
        ) from exc
    if not isinstance(body, dict) or body.get("ok") is not True:
        code = body.get("error_code") if isinstance(body, dict) else None
        if code == 429:
            raise DeliveryFailure(
                "Telegram rate limit", "queued", retry_after_seconds(body), "http_429"
            )
        raise DeliveryFailure(
            f"Telegram rejected message: {code}", "failed", outcome="rejected"
        )
    try:
        return int(body["result"]["message_id"])
    except (KeyError, ValueError, TypeError) as exc:
        raise DeliveryFailure(
            "Telegram response has no message ID",
            "uncertain",
            outcome="invalid_response",
        ) from exc


def deliver_once(database_url: str, token: str, *, limit: int = 20) -> int:
    delivered = 0
    for _ in range(limit):
        pending = claim_next(database_url)
        if pending is None:
            break
        with psycopg.connect(database_url) as connection:
            concert_id = connection.execute(
                "SELECT concert_id FROM notification_outbox WHERE id = %s",
                (pending.notification_id,),
            ).fetchone()[0]
            connection.execute(
                "SELECT 1 FROM concerts WHERE id = %s FOR UPDATE", (concert_id,)
            )
            row = connection.execute(
                "SELECT n.delivery_state, c.status, n.change_id = "
                "(SELECT max(ch.id) FROM concert_changes ch WHERE ch.concert_id = c.id), "
                "EXISTS (SELECT 1 FROM user_favorites f WHERE f.user_id = n.user_id "
                "AND f.concert_id = c.id), "
                "EXISTS (SELECT 1 FROM user_artist_subscriptions s JOIN concert_artists ca "
                "ON ca.artist_id = s.artist_id WHERE s.user_id = n.user_id "
                "AND ca.concert_id = c.id), u.notifications_enabled, u.bot_chat_available, "
                "(u.city IS NULL OR u.city = c.city), "
                "EXISTS (SELECT 1 FROM research_source_events source "
                "WHERE source.concert_id = c.id AND source.classification = 'concert' "
                "AND source.is_active), c.starts_at > now(), c.starts_at "
                "FROM notification_outbox n "
                "JOIN concerts c ON c.id = n.concert_id JOIN users u ON u.id = n.user_id "
                "WHERE n.id = %s "
                "FOR UPDATE OF n",
                (pending.notification_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Notification claim was lost before delivery")
            (
                delivery_state,
                status,
                current_change,
                favorite,
                subscribed,
                enabled,
                chat_available,
                city_matches,
                active_concert,
                future_concert,
                current_start,
            ) = row
            if delivery_state == "uncertain":
                continue
            if delivery_state != "sending":
                raise RuntimeError("Notification claim was lost before delivery")
            expected_status = {
                "cancelled": "cancelled",
                "restored": "scheduled",
                "rescheduled": "postponed",
            }.get(pending.kind)
            eligible = enabled and chat_available and city_matches and active_concert
            if pending.kind == "reminder":
                eligible = (
                    eligible
                    and favorite
                    and status != "cancelled"
                    and future_concert
                    and current_start == pending.starts_at
                )
            elif pending.kind == "new_concert":
                eligible = (
                    eligible
                    and subscribed
                    and status == "scheduled"
                    and future_concert
                    and current_start == pending.starts_at
                )
            else:
                eligible = eligible and (favorite or subscribed)
            if expected_status and (status != expected_status or not current_change):
                eligible = False
            if not eligible:
                connection.execute(
                    "UPDATE notification_outbox SET delivery_state = 'failed', "
                    "failure_reason = 'ineligible', last_error = 'No longer eligible before delivery' "
                    "WHERE id = %s",
                    (pending.notification_id,),
                )
                continue
            try:
                message_id = send_message(token, pending)
            except DeliveryFailure as exc:
                connection.execute(
                    "UPDATE notification_outbox SET delivery_state = %s, due_at = %s, "
                    "failure_reason = 'telegram', last_error = %s WHERE id = %s",
                    (
                        exc.state,
                        datetime.now(MOSCOW_TIME) + timedelta(seconds=exc.retry_after),
                        str(exc),
                        pending.notification_id,
                    ),
                )
                if exc.outcome == "forbidden":
                    connection.execute(
                        "UPDATE users SET bot_chat_available = false "
                        "WHERE id = (SELECT user_id FROM notification_outbox WHERE id = %s)",
                        (pending.notification_id,),
                    )
                NOTIFICATION_DELIVERIES.labels(pending.kind, exc.state).inc()
                TELEGRAM_ERRORS.labels(pending.kind, exc.outcome).inc()
                logger.log(
                    logging.WARNING if exc.state == "queued" else logging.ERROR,
                    "Notification delivery id=%d state=%s: %s",
                    pending.notification_id,
                    exc.state,
                    exc,
                )
            else:
                delay = connection.execute(
                    "UPDATE notification_outbox SET delivery_state = 'sent', sent_at = now(), "
                    "failure_reason = NULL, telegram_message_id = %s WHERE id = %s "
                    "RETURNING EXTRACT(EPOCH FROM (sent_at - due_at))",
                    (message_id, pending.notification_id),
                ).fetchone()
                NOTIFICATION_DELIVERIES.labels(pending.kind, "sent").inc()
                NOTIFICATION_DELAY.labels(pending.kind).observe(max(0, float(delay[0])))
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
    start_metrics_server()
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
