"""Build the three provisioned Grafana dashboards from reviewed queries"""

import json
from pathlib import Path

OUTPUT = Path(__file__).parent / "grafana" / "dashboards"


def panel(title, query, source="sql", kind="table", unit=None):
    return title, query, source, kind, unit


DASHBOARDS = {
    "afisha": (
        "Афиша и импорт",
        [
            panel(
                "Последняя успешная синхронизация по городам",
                "SELECT cities.city, s.last_successful_at, round(extract(epoch from now()-s.last_successful_at)/3600,1) AS age_hours, s.event_count AS received, s.rejected_count AS rejected FROM (VALUES ('Москва'), ('Санкт-Петербург')) cities(city) LEFT JOIN research_sync_state s ON s.source='timepad' AND s.city=cities.city ORDER BY cities.city",
            ),
            panel(
                "Попытки импорта по исходу",
                "sum by (city,outcome) (increase(gigradar_import_attempts_total[$__rate_interval]))",
                "prom",
                "timeseries",
            ),
            panel(
                "Длительность импорта p95",
                "histogram_quantile(0.95, sum by (city,outcome,le) (rate(gigradar_import_duration_seconds_bucket[$__rate_interval])))",
                "prom",
                "timeseries",
                "s",
            ),
            panel(
                "Получено и отклонено при импорте",
                "sum by (city,outcome) (increase(gigradar_import_records_total[$__range]))",
                "prom",
                "timeseries",
            ),
            panel(
                "Запросы Timepad по исходу",
                "sum by (outcome) (increase(gigradar_timepad_requests_total[$__range]))",
                "prom",
                "timeseries",
            ),
            panel(
                "Тайм-ауты и HTTP 429 Timepad",
                'sum by (outcome) (increase(gigradar_timepad_requests_total{outcome=~"timeout|http_429"}[$__range]))',
                "prom",
                "timeseries",
            ),
            panel(
                "Повторы Timepad",
                "sum by (city) (increase(gigradar_timepad_retries_total[$__range]))",
                "prom",
                "timeseries",
            ),
            panel(
                "Доступные концерты на 30 дней",
                "SELECT c.city, count(*) AS concerts FROM concerts c WHERE c.starts_at>=now() AND c.starts_at<now()+interval '30 days' AND c.status<>'cancelled' AND EXISTS (SELECT 1 FROM research_source_events s WHERE s.concert_id=c.id AND s.classification='concert') GROUP BY c.city ORDER BY c.city",
            ),
            panel(
                "Доля концертов с артистом",
                "SELECT c.city, count(*) AS concerts, count(*) FILTER (WHERE EXISTS (SELECT 1 FROM concert_artists ca WHERE ca.concert_id=c.id)) AS with_artist, round(100.0*count(*) FILTER (WHERE EXISTS (SELECT 1 FROM concert_artists ca WHERE ca.concert_id=c.id))/nullif(count(*),0),1) AS percent_with_artist FROM concerts c WHERE c.starts_at>=now() AND c.starts_at<now()+interval '30 days' AND c.status<>'cancelled' AND EXISTS (SELECT 1 FROM research_source_events s WHERE s.concert_id=c.id AND s.classification='concert') GROUP BY c.city",
            ),
            panel(
                "Неясные классификации",
                "SELECT city, count(*) AS unclear FROM research_source_events WHERE classification='unclear' AND starts_at>=now() AND starts_at<now()+interval '30 days' GROUP BY city",
            ),
            panel(
                "Артисты на проверку",
                "SELECT s.city, count(*) AS mentions_to_review FROM artist_mentions m JOIN research_source_events s ON (s.source,s.external_id)=(m.source,m.external_id) WHERE m.confidence='review' AND NOT m.reviewed AND s.starts_at>=now() GROUP BY s.city",
            ),
            panel(
                "Возможные дубли",
                "SELECT count(*) AS duplicate_candidates FROM duplicate_reviews",
            ),
            panel(
                "Давность last_seen_at у будущих записей",
                "SELECT city, count(*) AS source_records, count(*) FILTER (WHERE last_seen_at<now()-interval '12 hours') AS unseen_12h, count(*) FILTER (WHERE last_seen_at<now()-interval '24 hours') AS unseen_24h, round(max(extract(epoch from now()-last_seen_at))/3600,1) AS oldest_seen_hours FROM research_source_events WHERE source='timepad' AND classification='concert' AND starts_at>=now() AND starts_at<now()+interval '30 days' GROUP BY city",
            ),
        ],
    ),
    "notifications": (
        "Уведомления",
        [
            panel(
                "Очередь по типу и состоянию",
                "SELECT kind, delivery_state, count(*) AS messages FROM notification_outbox GROUP BY kind,delivery_state ORDER BY kind,delivery_state",
            ),
            panel(
                "Просроченные и самое старое",
                "SELECT kind, count(*) AS overdue, round(max(extract(epoch from now()-due_at))/60,1) AS oldest_overdue_minutes FROM notification_outbox WHERE delivery_state='queued' AND due_at<now() GROUP BY kind",
            ),
            panel(
                "Зависшие sending",
                "SELECT kind, count(*) AS stuck, round(max(extract(epoch from now()-claimed_at))/60,1) AS oldest_minutes FROM notification_outbox WHERE delivery_state='sending' AND claimed_at<now()-interval '5 minutes' GROUP BY kind",
            ),
            panel(
                "Неопределённая доставка",
                "SELECT kind, count(*) AS uncertain FROM notification_outbox WHERE delivery_state='uncertain' GROUP BY kind",
            ),
            panel(
                "Успешные доставки",
                "SELECT date_trunc('day',sent_at) AS \"time\", kind, count(*) AS delivered FROM notification_outbox WHERE delivery_state='sent' AND $__timeFilter(sent_at) GROUP BY 1,2 ORDER BY 1",
                "sql",
                "timeseries",
            ),
            panel(
                "Задержка от due_at до sent_at p95",
                "histogram_quantile(0.95, sum by (kind,le) (rate(gigradar_notification_delay_seconds_bucket[$__rate_interval])))",
                "prom",
                "timeseries",
                "s",
            ),
            panel(
                "Повторные попытки",
                "SELECT kind, delivery_state, count(*) AS retried FROM notification_outbox WHERE attempts>1 GROUP BY kind,delivery_state",
            ),
            panel(
                "Ошибки Telegram",
                "sum by (kind,outcome) (increase(gigradar_telegram_errors_total[$__range]))",
                "prom",
                "timeseries",
            ),
            panel(
                "Причины failed",
                "SELECT kind, coalesce(failure_reason, CASE WHEN last_error='No longer eligible' THEN 'ineligible' ELSE 'legacy_unknown' END) AS reason, count(*) AS messages FROM notification_outbox WHERE delivery_state='failed' GROUP BY kind,reason ORDER BY kind,reason",
            ),
        ],
    ),
    "product": (
        "Приложение и продукт",
        [
            panel(
                "Доступность процессов",
                'up{job=~"api|bot|collector|notifier|db-monitor"}',
                "prom",
                "timeseries",
            ),
            panel(
                "Доступность PostgreSQL", "gigradar_database_up", "prom", "timeseries"
            ),
            panel(
                "Проверка API /health", "gigradar_api_health_up", "prom", "timeseries"
            ),
            panel(
                "Запросы API по маршруту",
                "sum by (route,method) (rate(gigradar_http_requests_total[$__rate_interval]))",
                "prom",
                "timeseries",
            ),
            panel(
                "Ответы API 5xx",
                'sum by (route) (increase(gigradar_http_requests_total{status_class="5xx"}[$__range]))',
                "prom",
                "timeseries",
            ),
            panel(
                "Задержка API p95",
                "histogram_quantile(0.95, sum by (route,method,le) (rate(gigradar_http_request_duration_seconds_bucket[$__rate_interval])))",
                "prom",
                "timeseries",
                "s",
            ),
            panel(
                "Команды бота по исходу",
                "sum by (command,outcome) (increase(gigradar_bot_requests_total[$__range]))",
                "prom",
                "timeseries",
            ),
            panel(
                "Задержка команд бота p95",
                "histogram_quantile(0.95, sum by (command,le) (rate(gigradar_bot_request_duration_seconds_bucket[$__rate_interval])))",
                "prom",
                "timeseries",
                "s",
            ),
            panel(
                "Ошибки входа",
                "sum by (flow,outcome) (increase(gigradar_login_errors_total[$__range]))",
                "prom",
                "timeseries",
            ),
            panel(
                "Ошибки MusicBrainz",
                "sum by (outcome) (increase(gigradar_musicbrainz_errors_total[$__range]))",
                "prom",
                "timeseries",
            ),
            panel(
                "Новые пользователи",
                "SELECT date_trunc('day',created_at) AS \"time\", count(*) AS new_users FROM users WHERE $__timeFilter(created_at) GROUP BY 1 ORDER BY 1",
                "sql",
                "timeseries",
            ),
            panel(
                "Активные пользователи",
                'SELECT activity_date::timestamp AS "time", count(*) AS active_users FROM user_activity WHERE $__timeFilter(activity_date) GROUP BY 1 ORDER BY 1',
                "sql",
                "timeseries",
            ),
            panel(
                "Поиски с результатами и без",
                "SELECT date_trunc('day',occurred_at) AS \"time\", kind, outcome, count(*) AS searches FROM product_events WHERE kind IN ('concert_search','artist_search') AND $__timeFilter(occurred_at) GROUP BY 1,2,3 ORDER BY 1",
                "sql",
                "timeseries",
            ),
            panel(
                "Добавления и удаления",
                "SELECT date_trunc('day',occurred_at) AS \"time\", kind, count(*) AS actions FROM product_events WHERE kind IN ('subscription_add','subscription_remove','favorite_add','favorite_remove') AND $__timeFilter(occurred_at) GROUP BY 1,2 ORDER BY 1",
                "sql",
                "timeseries",
            ),
            panel(
                "Текущее число подписок и избранного",
                "SELECT (SELECT count(*) FROM user_artist_subscriptions) AS subscriptions, (SELECT count(*) FROM user_favorites) AS favorites, (SELECT count(DISTINCT concert_id) FROM user_favorites) AS distinct_favorite_concerts",
            ),
        ],
    ),
}


def build_dashboard(uid, title, definitions):
    panels = []
    for index, (label, query, source, kind, unit) in enumerate(definitions, 1):
        target = {"refId": "A"}
        if source == "prom":
            target.update({"expr": query, "legendFormat": ""})
        else:
            target.update(
                {
                    "rawSql": query,
                    "format": "time_series" if kind == "timeseries" else "table",
                }
            )
        panels.append(
            {
                "id": index,
                "title": label,
                "type": kind,
                "datasource": {
                    "type": "prometheus" if source == "prom" else "postgres",
                    "uid": "prometheus" if source == "prom" else "gigradar-postgres",
                },
                "targets": [target],
                "gridPos": {
                    "h": 8,
                    "w": 12,
                    "x": 12 * ((index - 1) % 2),
                    "y": 8 * ((index - 1) // 2),
                },
                "fieldConfig": {"defaults": {"unit": unit or "short"}, "overrides": []},
                "options": {"showHeader": True} if kind == "table" else {},
            }
        )
    return {
        "uid": f"gigradar-{uid}",
        "title": title,
        "schemaVersion": 41,
        "version": 1,
        "refresh": "1m",
        "time": {"from": "now-24h", "to": "now"},
        "panels": panels,
        "tags": ["GigRadar"],
    }


if __name__ == "__main__":
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for uid, (title, definitions) in DASHBOARDS.items():
        (OUTPUT / f"{uid}.json").write_text(
            json.dumps(
                build_dashboard(uid, title, definitions), ensure_ascii=False, indent=2
            )
            + "\n",
            encoding="utf-8",
        )
