"""Bounded-label operational metrics shared by GigRadar processes"""

import os

from prometheus_client import Counter, Histogram, start_http_server

HTTP_REQUESTS = Counter(
    "gigradar_http_requests_total", "API requests", ["route", "method", "status_class"]
)
HTTP_DURATION = Histogram(
    "gigradar_http_request_duration_seconds",
    "API request duration",
    ["route", "method"],
)
BOT_REQUESTS = Counter(
    "gigradar_bot_requests_total", "Bot updates", ["command", "outcome"]
)
BOT_DURATION = Histogram(
    "gigradar_bot_request_duration_seconds", "Bot update duration", ["command"]
)
IMPORT_ATTEMPTS = Counter(
    "gigradar_import_attempts_total", "Timepad import attempts", ["city", "outcome"]
)
IMPORT_DURATION = Histogram(
    "gigradar_import_duration_seconds", "Timepad import duration", ["city", "outcome"]
)
IMPORT_RECORDS = Counter(
    "gigradar_import_records_total", "Timepad records", ["city", "outcome"]
)
TIMEPAD_REQUESTS = Counter(
    "gigradar_timepad_requests_total", "Timepad HTTP requests", ["outcome"]
)
TIMEPAD_RETRIES = Counter("gigradar_timepad_retries_total", "Timepad retries", ["city"])
NOTIFICATION_DELIVERIES = Counter(
    "gigradar_notification_deliveries_total",
    "Committed delivery outcomes",
    ["kind", "outcome"],
)
NOTIFICATION_DELAY = Histogram(
    "gigradar_notification_delay_seconds", "Delay from due to sent", ["kind"]
)
TELEGRAM_ERRORS = Counter(
    "gigradar_telegram_errors_total", "Telegram delivery errors", ["kind", "outcome"]
)
MUSICBRAINZ_ERRORS = Counter(
    "gigradar_musicbrainz_errors_total", "MusicBrainz errors", ["outcome"]
)
LOGIN_ERRORS = Counter(
    "gigradar_login_errors_total", "Login errors", ["flow", "outcome"]
)


def start_metrics_server() -> None:
    if os.environ.get("GIGRADAR_METRICS_ENABLED") == "1":
        start_http_server(9000, addr="0.0.0.0")
