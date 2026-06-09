#!/usr/bin/env python3
"""Unit tests for the pure logic in check.py (timestamp parsing + backup-age).

Run: uv run pytest ansible/roles/containers/monitor-bridge/files
(or `uv run pytest` for the whole repo suite).

Covers the parts that can be wrong without a live deploy noticing — chiefly the
nanosecond RFC3339 parsing (Kopia emits 9 fractional digits; fromisoformat caps at 6)
and the Kopia /api/v1/sources age/error extraction. The HTTP glue is exercised live
via `check.py --once` at deploy time.
"""
from datetime import datetime, timezone, timedelta

import pytest

import check


def _seq(*values):
    """Return a callable that yields each value on successive calls (like mock side_effect)."""
    it = iter(values)
    return lambda *a, **k: next(it)


# --- parse_rfc3339 ----------------------------------------------------------

def test_nanosecond_precision_with_z():
    # Real Kopia value: 9 fractional digits + trailing Z
    dt = check.parse_rfc3339("2026-06-06T00:00:00.011699074Z")
    assert dt.tzinfo == timezone.utc
    assert dt.year == 2026
    assert dt.microsecond == 11699  # truncated from .011699074


def test_plain_z_no_fraction():
    dt = check.parse_rfc3339("2026-06-06T00:00:00Z")
    assert dt == datetime(2026, 6, 6, tzinfo=timezone.utc)


def test_offset_after_fraction():
    dt = check.parse_rfc3339("2026-06-06T01:00:00.123456789+01:00")
    assert dt.utcoffset().total_seconds() == 3600
    assert dt.microsecond == 123456


# --- backup_age_hours -------------------------------------------------------

NOW = datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc)


def _sources(last):
    return {
        "sources": [
            {"source": {"path": "/other"}, "lastSnapshot": {"startTime": "2020-01-01T00:00:00Z"}},
            {"source": {"path": "/data/containers"}, "lastSnapshot": last},
        ]
    }


def test_age_from_endtime_and_errors():
    last = {
        "startTime": "2026-06-06T00:00:00.5Z",
        "endTime": "2026-06-06T06:00:00.011699074Z",
        "stats": {"errorCount": 0},
    }
    age, errs = check.backup_age_hours(_sources(last), "/data/containers", now=NOW)
    assert age == pytest.approx(6.0, abs=0.01)  # 06:00 -> 12:00
    assert errs == 0


def test_error_count_surfaced():
    last = {"endTime": "2026-06-06T11:00:00Z", "stats": {"errorCount": 3}}
    age, errs = check.backup_age_hours(_sources(last), "/data/containers", now=NOW)
    assert errs == 3
    assert age == pytest.approx(1.0, abs=0.01)


def test_missing_source_raises():
    with pytest.raises(LookupError):
        check.backup_age_hours({"sources": []}, "/data/containers", now=NOW)


def test_no_snapshot_raises():
    src = {"sources": [{"source": {"path": "/data/containers"}, "lastSnapshot": None}]}
    with pytest.raises(LookupError):
        check.backup_age_hours(src, "/data/containers", now=NOW)


# --- prom_vector ------------------------------------------------------------

def _vector(*pairs):
    """Build a Prometheus instant-query JSON from (labels, value) pairs."""
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": labels, "value": [1700000000, str(val)]}
                for labels, val in pairs
            ],
        },
    }


def test_prom_vector_parses_labels_and_values(monkeypatch):
    payload = _vector(({"name": "sonarr"}, 5), ({"name": "radarr"}, 0))
    monkeypatch.setattr(check, "_get_json", lambda *a, **k: payload)
    out = check.prom_vector("whatever")
    assert out == [({"name": "sonarr"}, 5.0), ({"name": "radarr"}, 0.0)]


def test_prom_vector_empty_result_is_empty_list(monkeypatch):
    monkeypatch.setattr(check, "_get_json", lambda *a, **k: _vector())
    assert check.prom_vector("q") == []


def test_prom_vector_non_success_raises(monkeypatch):
    monkeypatch.setattr(check, "_get_json", lambda *a, **k: {"status": "error"})
    with pytest.raises(RuntimeError):
        check.prom_vector("q")


# --- check_restarts ---------------------------------------------------------

def test_restarts_names_containers_over_threshold(monkeypatch):
    vec = [({"name": "sonarr"}, 5.0), ({"name": "radarr"}, 1.0)]
    monkeypatch.setattr(check, "prom_vector", lambda *a, **k: vec)
    ok, msg = check.check_restarts()
    assert not ok
    assert "sonarr" in msg
    assert "radarr" not in msg  # 1 restart is under the default max of 3


def test_restarts_at_threshold_is_ok(monkeypatch):
    # default RESTART_MAX=3; exactly 3 must NOT alert (strictly greater)
    vec = [({"name": "sonarr"}, 3.0)]
    monkeypatch.setattr(check, "prom_vector", lambda *a, **k: vec)
    ok, _ = check.check_restarts()
    assert ok


def test_restarts_none_is_ok(monkeypatch):
    monkeypatch.setattr(check, "prom_vector", lambda *a, **k: [])
    ok, _ = check.check_restarts()
    assert ok


# --- check_oom --------------------------------------------------------------

def test_oom_names_killed_container(monkeypatch):
    vec = [({"name": "n8n"}, 2.0)]
    monkeypatch.setattr(check, "prom_vector", lambda *a, **k: vec)
    ok, msg = check.check_oom()
    assert not ok
    assert "n8n" in msg


def test_oom_none_is_ok(monkeypatch):
    monkeypatch.setattr(check, "prom_vector", lambda *a, **k: [])
    ok, _ = check.check_oom()
    assert ok


# --- check_cpu_throttle -----------------------------------------------------

def test_cpu_throttle_names_container_over_threshold(monkeypatch):
    # Alert needs BOTH ratio > CPU_THROTTLE_PCT (25%) AND cores lost > floor (0.05).
    # tdarr: 40% periods + 0.30 cores lost -> alerts. sonarr: 10% periods -> under ratio.
    ratio = [({"name": "tdarr"}, 0.40), ({"name": "sonarr"}, 0.10)]
    lost = [({"name": "tdarr"}, 0.30), ({"name": "sonarr"}, 0.30)]
    monkeypatch.setattr(check, "prom_vector", _seq(ratio, lost))
    ok, msg = check.check_cpu_throttle()
    assert not ok
    assert "tdarr" in msg
    assert "sonarr" not in msg  # 10% is under the default 25% threshold


def test_cpu_throttle_nan_is_ignored(monkeypatch):
    # unlimited container -> 0/0 -> NaN; NaN > threshold is False, so no alert
    ratio = [({"name": "jellyfin"}, float("nan"))]
    lost = [({"name": "jellyfin"}, 0.30)]
    monkeypatch.setattr(check, "prom_vector", _seq(ratio, lost))
    ok, _ = check.check_cpu_throttle()
    assert ok


def test_cpu_throttle_none_is_ok(monkeypatch):
    monkeypatch.setattr(check, "prom_vector", lambda *a, **k: [])
    ok, _ = check.check_cpu_throttle()
    assert ok


def test_cpu_throttle_below_cores_floor_is_ok(monkeypatch):
    # Real-world false positive: a 0.1-cpu sidecar throttled in 90% of its (few, bursty)
    # CFS periods but losing negligible absolute CPU time (0.0001 cores) must NOT alert.
    # 1st prom_vector call = throttle ratio, 2nd = throttled cores/s.
    ratio = [({"name": "monitor-bridge"}, 0.90)]
    lost = [({"name": "monitor-bridge"}, 0.0001)]
    monkeypatch.setattr(check, "prom_vector", _seq(ratio, lost))
    ok, _ = check.check_cpu_throttle()
    assert ok


# --- check_targets_down -----------------------------------------------------

def test_targets_names_down_target(monkeypatch):
    vec = [({"job": "node"}, 1.0), ({"job": "cadvisor"}, 0.0)]
    monkeypatch.setattr(check, "prom_vector", lambda *a, **k: vec)
    ok, msg = check.check_targets_down()
    assert not ok
    assert "cadvisor" in msg
    assert "node" not in msg


def test_targets_all_up_is_ok(monkeypatch):
    vec = [({"job": "node"}, 1.0), ({"job": "cadvisor"}, 1.0)]
    monkeypatch.setattr(check, "prom_vector", lambda *a, **k: vec)
    ok, _ = check.check_targets_down()
    assert ok


# --- check_traefik_5xx ------------------------------------------------------

def test_traefik_high_5xx_with_traffic_alerts(monkeypatch):
    # total 1.0 rps, 0.2 rps of 5xx -> 20% > 5%
    monkeypatch.setattr(check, "prom_scalar", _seq(1.0, 0.2))
    ok, msg = check.check_traefik_5xx()
    assert not ok
    assert "%" in msg


def test_traefik_high_ratio_below_floor_is_ok(monkeypatch):
    # 100% 5xx but only 0.01 rps (< 0.05 floor) -> must NOT alert
    monkeypatch.setattr(check, "prom_scalar", _seq(0.01, 0.01))
    ok, _ = check.check_traefik_5xx()
    assert ok


def test_traefik_low_5xx_is_ok(monkeypatch):
    monkeypatch.setattr(check, "prom_scalar", _seq(1.0, 0.01))
    ok, _ = check.check_traefik_5xx()
    assert ok


def test_traefik_no_traffic_metric_is_ok(monkeypatch):
    monkeypatch.setattr(check, "prom_scalar", _seq(None, None))
    ok, _ = check.check_traefik_5xx()
    assert ok


# --- check_mem --------------------------------------------------------------

def test_mem_reports_pct_without_oom(monkeypatch):
    # avail 2GB of 10GB -> 80% used, under default 90% -> ok, and no OOM wording
    calls = []
    values = iter([2e9, 10e9])

    def fake(*a, **k):
        calls.append(1)
        return next(values)

    monkeypatch.setattr(check, "prom_scalar", fake)
    ok, msg = check.check_mem()
    assert ok
    assert "OOM" not in msg
    assert len(calls) == 2  # only mem queries, no OOM query


def test_mem_high_alerts(monkeypatch):
    monkeypatch.setattr(check, "prom_scalar", _seq(0.5e9, 10e9))
    ok, msg = check.check_mem()
    assert not ok
    assert "mem" in msg.lower()


# --- parse_duration ---------------------------------------------------------

def test_parse_duration_units():
    assert check.parse_duration("900s") == 900
    assert check.parse_duration("15m") == 900
    assert check.parse_duration("1h") == 3600
    assert check.parse_duration("2d") == 172800
    assert check.parse_duration("300") == 300  # bare number = seconds


# --- n8n_failures -----------------------------------------------------------

N8N_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)


def _n8n_ago(minutes):
    return (N8N_NOW - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _workflows(*items):
    """items: (id, name, active) tuples -> n8n /workflows payload."""
    return {"data": [{"id": i, "name": n, "active": a} for i, n, a in items]}


def _executions(*items):
    """items: (workflowId, stoppedAt) tuples -> n8n /executions payload (all status=error)."""
    return {"data": [{"workflowId": w, "status": "error", "stoppedAt": s} for w, s in items]}


def test_n8n_failure_within_window_named():
    wf = _workflows(("1", "Prod Flow", True))
    ex = _executions(("1", _n8n_ago(5)))
    assert check.n8n_failures(wf, ex, 900, now=N8N_NOW) == [("Prod Flow", 1)]


def test_n8n_failure_outside_window_ignored():
    wf = _workflows(("1", "Prod Flow", True))
    ex = _executions(("1", _n8n_ago(30)))  # 30m ago, window 15m
    assert check.n8n_failures(wf, ex, 900, now=N8N_NOW) == []


def test_n8n_inactive_workflow_ignored():
    wf = _workflows(("1", "Draft Flow", False))
    ex = _executions(("1", _n8n_ago(5)))
    assert check.n8n_failures(wf, ex, 900, now=N8N_NOW) == []


def test_n8n_multiple_failures_counted_and_sorted():
    wf = _workflows(("1", "A Flow", True), ("2", "B Flow", True))
    ex = _executions(
        ("1", _n8n_ago(2)),
        ("2", _n8n_ago(3)), ("2", _n8n_ago(4)), ("2", _n8n_ago(5)),
    )
    # B has 3 failures, A has 1 -> sorted by count desc
    assert check.n8n_failures(wf, ex, 900, now=N8N_NOW) == [("B Flow", 3), ("A Flow", 1)]


def test_n8n_empty_inputs():
    assert check.n8n_failures({"data": []}, {"data": []}, 900, now=N8N_NOW) == []


def test_n8n_missing_stoppedat_falls_back_to_startedat():
    wf = _workflows(("1", "Prod Flow", True))
    ex = {"data": [{"workflowId": "1", "status": "error", "startedAt": _n8n_ago(5)}]}
    assert check.n8n_failures(wf, ex, 900, now=N8N_NOW) == [("Prod Flow", 1)]


def test_n8n_naive_timestamp_treated_as_utc():
    # n8n normally emits UTC 'Z'; a naive timestamp must not raise on the tz-aware compare
    wf = _workflows(("1", "Prod Flow", True))
    naive = (N8N_NOW - timedelta(minutes=5)).replace(tzinfo=None).isoformat()  # no offset/Z
    ex = {"data": [{"workflowId": "1", "status": "error", "stoppedAt": naive}]}
    assert check.n8n_failures(wf, ex, 900, now=N8N_NOW) == [("Prod Flow", 1)]


# --- check_n8n --------------------------------------------------------------

def test_n8n_disabled_without_key():
    # N8N_API_KEY defaults to "" in tests -> monitoring disabled, never a false page
    ok, msg = check.check_n8n()
    assert ok
    assert "disabled" in msg.lower()


def test_n8n_check_down_on_recent_failure(monkeypatch):
    monkeypatch.setattr(check, "N8N_API_KEY", "x")
    wf = {"data": [{"id": "1", "name": "Prod Flow", "active": True}]}
    now_iso = datetime.now(timezone.utc).isoformat()
    ex = {"data": [{"workflowId": "1", "status": "error", "stoppedAt": now_iso}]}
    monkeypatch.setattr(check, "_get_json", _seq(wf, ex))
    ok, msg = check.check_n8n()
    assert not ok
    assert "Prod Flow" in msg


def test_n8n_check_ok_when_no_failures(monkeypatch):
    monkeypatch.setattr(check, "N8N_API_KEY", "x")
    wf = {"data": [{"id": "1", "name": "Prod Flow", "active": True}]}
    ex = {"data": []}
    monkeypatch.setattr(check, "_get_json", _seq(wf, ex))
    ok, msg = check.check_n8n()
    assert ok
    assert "no active-workflow failures" in msg


def test_n8n_check_at_threshold_is_ok(monkeypatch):
    # total failures == N8N_FAIL_MAX must NOT alert (strictly greater)
    monkeypatch.setattr(check, "N8N_API_KEY", "x")
    monkeypatch.setattr(check, "N8N_FAIL_MAX", 1.0)
    wf = {"data": [{"id": "1", "name": "Prod Flow", "active": True}]}
    now_iso = datetime.now(timezone.utc).isoformat()
    ex = {"data": [{"workflowId": "1", "status": "error", "stoppedAt": now_iso}]}
    monkeypatch.setattr(check, "_get_json", _seq(wf, ex))
    ok, _ = check.check_n8n()
    assert ok


# --- gitops_alive / gitops_status (pure) ------------------------------------

def test_gitops_alive_fresh():
    ok, msg = check.gitops_alive(60, 5400)
    assert ok
    assert "1m ago" in msg

def test_gitops_alive_at_threshold_is_ok():
    # exactly at max age still counts as alive (<=)
    ok, _ = check.gitops_alive(5400, 5400)
    assert ok

def test_gitops_alive_stale():
    ok, msg = check.gitops_alive(6000, 5400)  # 100m > 90m
    assert not ok
    assert "100m ago" in msg

def test_gitops_status_no_hold():
    ok, msg = check.gitops_status(None)
    assert ok
    assert msg == "no held deploy"

def test_gitops_status_empty_is_ok():
    ok, _ = check.gitops_status("")
    assert ok

def test_gitops_status_held_names_sha():
    ok, msg = check.gitops_status("abc123def4567890")
    assert not ok
    assert "abc123de" in msg
