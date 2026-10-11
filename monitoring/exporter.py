"""Expose database health and alert inputs without exporting individual records"""

from __future__ import annotations

import os
from urllib.error import URLError
from urllib.request import urlopen

import psycopg
from prometheus_client import REGISTRY, start_http_server
from prometheus_client.core import GaugeMetricFamily

CITIES = ("Москва", "Санкт-Петербург")


class DatabaseSnapshot:
    def collect(self):
        health_url = os.environ.get("API_HEALTH_URL")
        if health_url:
            api_health = GaugeMetricFamily(
                "gigradar_api_health_up", "API health endpoint responded successfully"
            )
            try:
                with urlopen(health_url, timeout=3) as response:
                    api_health.add_metric([], int(response.status == 200))
            except (URLError, TimeoutError):
                api_health.add_metric([], 0)
            yield api_health
        healthy = GaugeMetricFamily("gigradar_database_up", "Monitoring SQL succeeded")
        try:
            with psycopg.connect(
                host=os.environ["PGHOST"],
                dbname=os.environ["PGDATABASE"],
                user=os.environ["PGUSER"],
                password=os.environ["PGPASSWORD"],
                connect_timeout=3,
            ) as database:
                sync_rows = database.execute(
                    "SELECT city, EXTRACT(EPOCH FROM (now() - last_successful_at)) "
                    "FROM research_sync_state WHERE source = 'timepad'"
                ).fetchall()
                notification_rows = database.execute(
                    "SELECT "
                    "count(*) FILTER (WHERE delivery_state = 'sending' AND claimed_at < now() - interval '5 minutes'), "
                    "count(*) FILTER (WHERE delivery_state = 'queued' AND due_at < now() - interval '30 minutes'), "
                    "count(*) FILTER (WHERE delivery_state = 'uncertain') "
                    "FROM notification_outbox"
                ).fetchone()
        except (psycopg.Error, KeyError):
            healthy.add_metric([], 0)
            yield healthy
            return

        healthy.add_metric([], 1)
        yield healthy
        presence = GaugeMetricFamily(
            "gigradar_import_state_present", "Timepad sync row exists", labels=["city"]
        )
        age = GaugeMetricFamily(
            "gigradar_import_success_age_seconds",
            "Age of last successful city import",
            labels=["city"],
        )
        known = dict(sync_rows)
        for city in CITIES:
            presence.add_metric([city], int(city in known))
            if city in known:
                age.add_metric([city], float(known[city]))
        yield presence
        yield age
        for metric_name, description, count in zip(
            (
                "gigradar_stuck_sending",
                "gigradar_overdue_queued",
                "gigradar_uncertain_notifications",
            ),
            (
                "Sending older than five minutes",
                "Queued overdue by thirty minutes",
                "Uncertain deliveries",
            ),
            notification_rows,
            strict=True,
        ):
            gauge = GaugeMetricFamily(metric_name, description)
            gauge.add_metric([], count)
            yield gauge


if __name__ == "__main__":
    REGISTRY.register(DatabaseSnapshot())
    start_http_server(9100, addr="0.0.0.0")
    from threading import Event

    Event().wait()
