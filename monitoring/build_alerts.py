"""Build provisioned Grafana alerts from bounded operational series"""

import json
from pathlib import Path

API_5XX = '(sum(increase(gigradar_http_requests_total{status_class="5xx"}[10m])) or on() (0 * max(up{job="api"})))'
API_ALL = '(sum(increase(gigradar_http_requests_total[10m])) or on() (0 * max(up{job="api"})))'

ALERTS = [
    (
        "import-old",
        "Нет успешного импорта города более 12 часов",
        "gigradar_import_success_age_seconds > bool 43200",
        "2m",
    ),
    (
        "import-row-missing",
        "Нет строки состояния импорта",
        "gigradar_import_state_present == bool 0",
        "2m",
    ),
    (
        "sending-stuck",
        "Уведомление sending старше 5 минут",
        "gigradar_stuck_sending > bool 0",
        "1m",
    ),
    (
        "queued-overdue",
        "Уведомление queued просрочено более 30 минут",
        "gigradar_overdue_queued > bool 0",
        "1m",
    ),
    (
        "uncertain",
        "Появилась неопределённая доставка",
        "gigradar_uncertain_notifications > bool 0",
        "0s",
    ),
    ("api-down", "API недоступен", "gigradar_api_health_up == bool 0", "2m"),
    ("database-down", "PostgreSQL недоступен", "gigradar_database_up == bool 0", "2m"),
    ("collector-down", "Импортёр недоступен", 'up{job="collector"} == bool 0', "5m"),
    ("monitor-down", "Монитор БД недоступен", 'up{job="db-monitor"} == bool 0', "2m"),
    (
        "monitor-data-missing",
        "Пропали данные мониторинга",
        'absent_over_time(gigradar_database_up[5m]) or absent_over_time(up{job="api"}[5m]) or absent_over_time(up{job="collector"}[5m])',
        "0s",
    ),
    (
        "api-errors",
        "Повышены ошибки API при малом трафике",
        f"({API_5XX} >= bool 3) * ({API_5XX} / clamp_min({API_ALL}, 1) >= bool 0.2)",
        "2m",
    ),
]


def alert(uid, title, expr, duration):
    return {
        "uid": uid,
        "title": title,
        "condition": "C",
        "for": duration,
        "noDataState": "OK" if uid == "monitor-data-missing" else "Alerting",
        "execErrState": "Alerting",
        "annotations": {"summary": title},
        "labels": {"service": "gigradar"},
        "data": [
            {
                "refId": "A",
                "relativeTimeRange": {"from": 600, "to": 0},
                "datasourceUid": "prometheus",
                "model": {
                    "datasource": {"type": "prometheus", "uid": "prometheus"},
                    "expr": expr,
                    "instant": True,
                    "range": False,
                    "intervalMs": 1000,
                    "maxDataPoints": 43200,
                    "refId": "A",
                },
            },
            {
                "refId": "B",
                "relativeTimeRange": {"from": 0, "to": 0},
                "datasourceUid": "__expr__",
                "model": {
                    "type": "reduce",
                    "expression": "A",
                    "reducer": "last",
                    "settings": {"mode": ""},
                    "refId": "B",
                },
            },
            {
                "refId": "C",
                "relativeTimeRange": {"from": 0, "to": 0},
                "datasourceUid": "__expr__",
                "model": {
                    "type": "threshold",
                    "expression": "B",
                    "conditions": [
                        {
                            "evaluator": {"type": "gt", "params": [0]},
                            "operator": {"type": "and"},
                            "query": {"params": ["C"]},
                            "reducer": {"type": "last", "params": []},
                            "type": "query",
                        }
                    ],
                    "refId": "C",
                },
            },
        ],
    }


if __name__ == "__main__":
    destination = (
        Path(__file__).parent / "grafana" / "provisioning" / "alerting" / "rules.yaml"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    rules = {
        "apiVersion": 1,
        "groups": [
            {
                "orgId": 1,
                "name": "GigRadar",
                "folder": "GigRadar",
                "interval": "1m",
                "rules": [alert(*definition) for definition in ALERTS],
            }
        ],
    }
    destination.write_text(
        json.dumps(rules, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
