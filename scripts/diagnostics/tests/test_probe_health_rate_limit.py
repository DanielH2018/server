"""The health gate's Prometheus read behind the query door's Traefik rate limit (#7721).

Run: uv run pytest scripts/diagnostics/tests/test_probe_health_rate_limit.py
"""

import json

import pytest

from deploy_tools.deploy_lib import detach_notify
from diagnostics.probe_lib import core, health_alerts
from lib.exit_codes import PROBE_UNREADABLE

TAG = "littlelink"
APPLIED = 1_000_000.0
NO_ALERTS = json.dumps({"status": "success", "data": {"result": []}})


class ThrottlingEdge:
    """A Prometheus with no firing alerts behind an edge that answers 429 `throttled` times.

    The window's clock starts at `elapsed_s` and advances only when a retry sleeps.
    """

    def __init__(self, throttled, elapsed_s=0):
        self.throttled = throttled
        self.requests = 0
        self.slept = []
        self.now = elapsed_s
        self.window = health_alerts.RetryWindow(
            health_alerts.RETRY_CUTOFF_S, clock=lambda: self.now, start=0
        )

    def get(self, url, resolve=None):
        self.requests += 1
        if self.requests <= self.throttled:
            return 429, "Too Many Requests\n"
        return 200, NO_ALERTS

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def query(self, promql):
        return health_alerts.prom_query(
            promql,
            endpoint=lambda: ("https://prometheus.example", None),
            get=self.get,
            sleep=self.sleep,
            window=self.window,
        )

    def gate(self):
        tools = health_alerts.PromTools(
            query=self.query, sleep=lambda s: None, clock=lambda: APPLIED + 30
        )
        return health_alerts.gate(TAG, APPLIED, APPLIED, False, tools)


def test_a_429_twice_then_200_is_graded():
    edge = ThrottlingEdge(throttled=2)
    code, _ = edge.gate()
    assert code == 0
    assert edge.slept == [2, 4]


def test_a_429_that_outlasts_the_retries_defers_with_the_edge_s_words(capsys):
    edge = ThrottlingEdge(throttled=10**6)
    with pytest.raises(core.Unreadable) as exc:
        edge.gate()
    assert exc.value.code == PROBE_UNREADABLE
    assert capsys.readouterr().err.strip().splitlines()[-1] == "Too Many Requests"


def test_a_429_past_the_retry_cutoff_is_not_retried():
    edge = ThrottlingEdge(throttled=10**6, elapsed_s=health_alerts.RETRY_CUTOFF_S - 1)
    with pytest.raises(core.Unreadable):
        edge.query("up")
    assert (edge.requests, edge.slept) == (1, [])


def test_the_waits_after_the_cutoff_fit_inside_the_probe_timeout():
    assert health_alerts.PROBE_TIMEOUT_S == detach_notify.PROBE_TIMEOUT_S
    tail = 2 * health_alerts.PROBE_WAIT_S + health_alerts.REREAD_WAIT_S
    assert 0 < health_alerts.RETRY_CUTOFF_S < health_alerts.PROBE_TIMEOUT_S - tail
