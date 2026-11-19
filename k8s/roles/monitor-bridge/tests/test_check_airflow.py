"""The Airflow scheduler deadman: the api-server's health payload, read through a `FakeSources`.

The payloads follow Airflow 3's `airflow/api/common/airflow_health.py:get_airflow_health`.
"""

from dataclasses import replace
from datetime import datetime, timezone

import checks.service
from _fake_sources import FakeSources

NOW = datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc)
FRESH = "2026-11-19T11:59:48.123456+00:00"


def _health(status, beat, metadatabase="healthy"):
    return {
        "metadatabase": {"status": metadatabase},
        "scheduler": {"status": status, "latest_scheduler_heartbeat": beat},
        "triggerer": {"status": None, "latest_triggerer_heartbeat": None},
        "dag_processor": {"status": "healthy", "latest_dag_processor_heartbeat": FRESH},
    }


def _check(cfg, payload):
    src = FakeSources(get_json=lambda url: payload)
    return checks.service.check_airflow_scheduler(cfg, src, now=NOW), src.calls


def test_a_healthy_scheduler_is_up_and_the_read_is_the_health_endpoint(cfg):
    (ok, msg), calls = _check(cfg, _health("healthy", FRESH))
    assert ok is True
    assert "last heartbeat 12s ago" in msg
    assert calls == [
        (
            "get_json",
            ("http://airflow.homelab.svc.cluster.local:8080/api/v2/monitor/health",),
        )
    ]


def test_a_stopped_scheduler_with_a_fresh_heartbeat_is_down(cfg):
    """A clean stop leaves the job's last heartbeat fresh but its state no longer RUNNING."""
    (ok, msg), _ = _check(cfg, _health("unhealthy", FRESH))
    assert ok is False
    assert msg == "scheduler unhealthy, last heartbeat 12s ago"


def test_a_scheduler_with_no_heartbeat_on_record_is_down(cfg):
    (ok, msg), _ = _check(cfg, _health("unhealthy", None))
    assert ok is False
    assert "no heartbeat on record" in msg


def test_an_unreadable_metadata_database_is_down_and_named(cfg):
    (ok, msg), _ = _check(cfg, _health("unhealthy", None, metadatabase="unhealthy"))
    assert ok is False
    assert "metadata database" in msg


def test_an_empty_url_disables_the_check_without_a_read(cfg):
    ok, msg = checks.service.check_airflow_scheduler(
        replace(cfg, AIRFLOW_URL=""), FakeSources(), now=NOW
    )
    assert ok is True
    assert "disabled" in msg
