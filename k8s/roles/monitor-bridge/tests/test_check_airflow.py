"""The Airflow scheduler deadman, read through a `FakeSources` standing in for its health server.

The server is Airflow 3's `airflow/utils/scheduler_health.py:HealthServer`: an empty 200 while
the scheduler is alive, and an error status otherwise.
"""

import email.message
import urllib.error
from dataclasses import replace

import pytest

import checks.service
from _fake_sources import FakeSources

HEALTH = "http://airflow-scheduler.homelab.svc.cluster.local:8974/health"


def _refusing(status):
    def get_bytes(url):
        err = urllib.error.HTTPError(url, status, "x", email.message.Message(), None)
        err.close()  # bridge.net reads and closes the body before re-raising
        raise err

    return get_bytes


def test_a_healthy_scheduler_is_up_and_the_read_is_its_health_server(cfg):
    src = FakeSources(get_bytes=lambda url: b"")
    ok, _ = checks.service.check_airflow_scheduler(cfg, src)
    assert ok is True
    assert src.calls == [("get_bytes", (HEALTH,))]


def test_a_scheduler_that_reports_itself_unhealthy_is_down(cfg):
    ok, msg = checks.service.check_airflow_scheduler(
        cfg, FakeSources(get_bytes=_refusing(503))
    )
    assert ok is False
    assert "scheduler unhealthy" in msg


def test_any_other_http_error_reaches_evaluate(cfg):
    with pytest.raises(urllib.error.HTTPError):
        checks.service.check_airflow_scheduler(
            cfg, FakeSources(get_bytes=_refusing(404))
        )


def test_an_empty_url_disables_the_check_without_a_read(cfg):
    ok, msg = checks.service.check_airflow_scheduler(
        replace(cfg, AIRFLOW_SCHEDULER_URL=""), FakeSources()
    )
    assert ok is True
    assert "disabled" in msg
