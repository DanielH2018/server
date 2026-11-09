"""The health gate's Prometheus read behind the query door's Traefik rate limit (#7721).

Run: uv run pytest scripts/diagnostics/tests/test_probe_health_rate_limit.py
"""

import json

import pytest

from diagnostics.probe_lib import core, health_alerts
from lib.exit_codes import PROBE_UNREADABLE

TAG = "littlelink"
APPLIED = 1_000_000.0


class ThrottlingEdge:
    """A Prometheus with no firing alerts behind an edge that answers 429 `throttled` times."""

    def __init__(self, throttled, budget_s=health_alerts.RETRY_BUDGET_S):
        self.throttled = throttled
        self.requests = 0
        self.slept = []
        self.budget = health_alerts.RetryBudget(budget_s)

    def get(self, url, resolve=None):
        self.requests += 1
        if self.requests <= self.throttled:
            return 429, "Too Many Requests\n"
        return 200, json.dumps({"status": "success", "data": {"result": []}})

    def query(self, promql):
        return health_alerts.prom_query(
            promql,
            endpoint=lambda: ("https://prometheus.example", None),
            get=self.get,
            sleep=self.slept.append,
            budget=self.budget,
        )

    def tools(self):
        return health_alerts.PromTools(
            query=self.query, sleep=lambda s: None, clock=lambda: APPLIED + 30
        )


def test_a_429_twice_then_200_is_graded():
    edge = ThrottlingEdge(throttled=2)
    code, _ = health_alerts.gate(TAG, APPLIED, APPLIED, False, edge.tools())
    assert code == 0
    assert edge.slept == [2, 4]


def test_a_429_that_outlasts_the_retries_defers_with_the_edge_s_words(capsys):
    edge = ThrottlingEdge(throttled=10**6)
    with pytest.raises(core.Unreadable) as exc:
        health_alerts.gate(TAG, APPLIED, APPLIED, False, edge.tools())
    assert exc.value.code == PROBE_UNREADABLE
    assert capsys.readouterr().err.strip().splitlines()[-1] == "Too Many Requests"
    assert sum(edge.slept) == health_alerts.RETRY_BUDGET_S


def test_a_spent_retry_budget_sends_one_request():
    edge = ThrottlingEdge(throttled=10**6, budget_s=1)
    with pytest.raises(core.Unreadable):
        edge.query("up")
    assert (edge.requests, edge.slept) == (1, [])
