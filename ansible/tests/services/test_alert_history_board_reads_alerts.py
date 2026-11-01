#!/usr/bin/env python3
"""The Alert History board reads Prometheus's `ALERTS`, grouped by `service` (M6 B-5).

Before B-5 the board rebuilt Uptime Kuma DOWN history from Loki log lines. M6 retires Kuma, so
the board reads the one series every alert rule writes. A panel left on a Loki query, or one
that drops the `$service` filter, renders without error and answers a different question than
its title, so each panel query is held to four things: the Prometheus datasource, `ALERTS`,
firing state only, and the `$service` variable. `Watchdog` always fires, so the board leaves
it out.

Paired, per the repo's red-proof rule: the real board must come back clean, and a Loki target
like the one B-5 removed must not.

Run: uv run pytest ansible/tests/services/test_alert_history_board_reads_alerts.py
"""

import json

from _helpers import ROLES

BOARD = ROLES / "k8s/observability/files/dashboards/Infrastructure/alert-history.json"
PROMETHEUS_UID = "prometheus"

# Named rather than counted, so a dropped or retitled panel fails here instead of leaving the
# checks below to pass over fewer panels.
PANELS = frozenset(
    {
        "Firing minutes by service (range total)",
        "Firing alerts over time, by service",
        "Alerts that fired in the range",
        "Firing now",
    }
)

REQUIRED_TERMS = (
    "ALERTS{",
    'alertstate="firing"',
    'alertname!="Watchdog"',
    'service=~"$service"',
)


def _board() -> dict:
    return json.loads(BOARD.read_text())


def _targets(doc: dict) -> list[tuple[str, dict]]:
    return [(p["title"], t) for p in doc["panels"] for t in p.get("targets", [])]


def problems(doc: dict) -> list[str]:
    """One line per panel query that does not read firing `ALERTS` for the chosen services."""
    found = []
    for title, target in _targets(doc):
        if target.get("datasource", {}).get("uid") != PROMETHEUS_UID:
            found.append(f"{title}: datasource is {target.get('datasource')}")
        expr = target.get("expr", "")
        found += [f"{title}: no {term}" for term in REQUIRED_TERMS if term not in expr]
        if "sum by (" in expr or "max by (" in expr:
            if "by (service" not in expr:
                found.append(f"{title}: aggregates without service first")
    return found


def test_the_board_holds_the_named_panels():
    assert {p["title"] for p in _board()["panels"]} == PANELS


def test_every_panel_query_reads_firing_alerts_by_service():
    doc = _board()

    assert len(_targets(doc)) >= len(PANELS)
    assert problems(doc) == []


def test_a_loki_target_like_the_kuma_reconstruction_is_flagged():
    doc = {
        "panels": [
            {
                "title": "DOWN cycles by check (range total)",
                "targets": [
                    {
                        "datasource": {"type": "loki", "uid": "bf4q19tuivta8e"},
                        "expr": 'sum by (name) (count_over_time({container="monitor-bridge"} '
                        "|~ `^DOWN ` [$__range]))",
                    }
                ],
            }
        ]
    }

    found = problems(doc)

    assert any("datasource" in line for line in found), found
    assert any("no ALERTS{" in line for line in found), found
    assert any("without service first" in line for line in found), found


def test_the_service_variable_lists_the_alerts_service_values():
    [variable] = _board()["templating"]["list"]

    assert variable["name"] == "service"
    assert variable["definition"] == "label_values(ALERTS, service)"
    assert variable["includeAll"] and variable["allValue"] == ".*"
