#!/usr/bin/env python3
"""Unit tests for the pure logic in check.py (timestamp parsing + backup-age).

Run: uv run pytest ansible/roles/containers/monitor-bridge/files
(or `uv run pytest` for the whole repo suite).

Covers the parts that can be wrong without a live deploy noticing — chiefly the
nanosecond RFC3339 parsing (Kopia emits 9 fractional digits; fromisoformat caps at 6)
and the Kopia /api/v1/sources age/error extraction. The HTTP glue is exercised live
via `check.py --once` at deploy time.
"""

import os
import time
import urllib.error
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
            {
                "source": {"path": "/other"},
                "lastSnapshot": {"startTime": "2020-01-01T00:00:00Z"},
            },
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


def _cpu_cycle(monkeypatch, ratio, lost):
    """Feed one loop iteration's two prom_vector calls and run the check."""
    monkeypatch.setattr(check, "prom_vector", _seq(ratio, lost))
    return check.check_cpu_throttle()


BREACH_RATIO = [({"name": "tdarr"}, 0.40), ({"name": "sonarr"}, 0.10)]
BREACH_LOST = [({"name": "tdarr"}, 0.30), ({"name": "sonarr"}, 0.30)]


def test_cpu_throttle_single_breach_is_suppressed(monkeypatch):
    # One breaching cycle (a short burst) must NOT alert — sustained throttling only.
    # The up-msg still names the offender so the bridge log keeps the evidence.
    monkeypatch.setattr(check, "_cpu_breach_streak", 0)
    ok, msg = _cpu_cycle(monkeypatch, BREACH_RATIO, BREACH_LOST)
    assert ok
    assert "tdarr" in msg
    assert "1/3" in msg  # streak progress vs default CPU_CONSECUTIVE=3


def test_cpu_throttle_sustained_breach_alerts_and_names(monkeypatch):
    # Default CPU_CONSECUTIVE=3: the 3rd consecutive breaching cycle (~15 min at the
    # 5-min loop) goes down, naming the offender. sonarr stays under the ratio gate.
    monkeypatch.setattr(check, "_cpu_breach_streak", 0)
    for _ in range(2):
        ok, _ = _cpu_cycle(monkeypatch, BREACH_RATIO, BREACH_LOST)
        assert ok
    ok, msg = _cpu_cycle(monkeypatch, BREACH_RATIO, BREACH_LOST)
    assert not ok
    assert "tdarr" in msg
    assert "sonarr" not in msg  # 10% is under the default 25% threshold


def test_cpu_throttle_clean_cycle_resets_streak(monkeypatch):
    # breach, breach, clean, breach -> never down (the streak restarts after the gap)
    monkeypatch.setattr(check, "_cpu_breach_streak", 0)
    assert _cpu_cycle(monkeypatch, BREACH_RATIO, BREACH_LOST)[0]
    assert _cpu_cycle(monkeypatch, BREACH_RATIO, BREACH_LOST)[0]
    monkeypatch.setattr(check, "prom_vector", lambda *a, **k: [])
    assert check.check_cpu_throttle()[0]
    assert _cpu_cycle(monkeypatch, BREACH_RATIO, BREACH_LOST)[0]


def test_cpu_throttle_stays_down_while_breaching(monkeypatch):
    # Once the streak crosses the threshold, every further breaching cycle is down too
    # (no flapping back to up until a clean cycle).
    monkeypatch.setattr(check, "_cpu_breach_streak", 0)
    for _ in range(3):
        ok, _ = _cpu_cycle(monkeypatch, BREACH_RATIO, BREACH_LOST)
    assert not ok
    ok, msg = _cpu_cycle(monkeypatch, BREACH_RATIO, BREACH_LOST)
    assert not ok
    assert "tdarr" in msg


def test_cpu_throttle_nan_is_ignored(monkeypatch):
    # unlimited container -> 0/0 -> NaN; NaN > threshold is False, so no alert
    monkeypatch.setattr(check, "_cpu_breach_streak", 0)
    ratio = [({"name": "jellyfin"}, float("nan"))]
    lost = [({"name": "jellyfin"}, 0.30)]
    ok, _ = _cpu_cycle(monkeypatch, ratio, lost)
    assert ok


def test_cpu_throttle_none_is_ok(monkeypatch):
    monkeypatch.setattr(check, "_cpu_breach_streak", 0)
    monkeypatch.setattr(check, "prom_vector", lambda *a, **k: [])
    ok, _ = check.check_cpu_throttle()
    assert ok


def test_cpu_throttle_below_cores_floor_is_ok(monkeypatch):
    # Real-world false positive: a 0.1-cpu sidecar throttled in 90% of its (few, bursty)
    # CFS periods but losing negligible absolute CPU time (0.0001 cores) must NOT alert.
    # 1st prom_vector call = throttle ratio, 2nd = throttled cores/s.
    monkeypatch.setattr(check, "_cpu_breach_streak", 0)
    ratio = [({"name": "monitor-bridge"}, 0.90)]
    lost = [({"name": "monitor-bridge"}, 0.0001)]
    ok, _ = _cpu_cycle(monkeypatch, ratio, lost)
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
# Per-service: 1st prom_vector call = total rps by service, 2nd = 5xx rps by service.


def test_traefik_names_erroring_service(monkeypatch):
    # sonarr: 0.2 of 1.0 rps = 20% 5xx -> named. jellyfin: clean -> not named.
    total = [({"service": "sonarr@docker"}, 1.0), ({"service": "jellyfin@docker"}, 2.0)]
    errs = [({"service": "sonarr@docker"}, 0.2)]
    monkeypatch.setattr(check, "prom_vector", _seq(total, errs))
    ok, msg = check.check_traefik_5xx()
    assert not ok
    assert "sonarr@docker" in msg
    assert "jellyfin" not in msg


def test_traefik_low_traffic_service_cannot_hide_behind_busy_one(monkeypatch):
    # The old aggregate ratio diluted a broken low-traffic service below 5%:
    # 0.06 rps all-5xx next to 9.94 rps clean = 0.6% aggregate. Per-service it alerts.
    total = [({"service": "broken@docker"}, 0.06), ({"service": "busy@docker"}, 9.94)]
    errs = [({"service": "broken@docker"}, 0.06)]
    monkeypatch.setattr(check, "prom_vector", _seq(total, errs))
    ok, msg = check.check_traefik_5xx()
    assert not ok
    assert "broken@docker" in msg


def test_traefik_high_ratio_below_floor_is_ok(monkeypatch):
    # 100% 5xx but only 0.01 rps (< 0.05 per-service floor) -> must NOT alert
    total = [({"service": "quiet@docker"}, 0.01)]
    errs = [({"service": "quiet@docker"}, 0.01)]
    monkeypatch.setattr(check, "prom_vector", _seq(total, errs))
    ok, _ = check.check_traefik_5xx()
    assert ok


def test_traefik_low_5xx_is_ok(monkeypatch):
    total = [({"service": "sonarr@docker"}, 1.0)]
    errs = [({"service": "sonarr@docker"}, 0.01)]  # 1% < 5%
    monkeypatch.setattr(check, "prom_vector", _seq(total, errs))
    ok, _ = check.check_traefik_5xx()
    assert ok


def test_traefik_no_traffic_is_ok(monkeypatch):
    monkeypatch.setattr(check, "prom_vector", lambda *a, **k: [])
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


# --- check_backup -----------------------------------------------------------


def _iso_ago(hours):
    """RFC3339 'Z' timestamp `hours` in the past. check_backup() (unlike the
    backup_age_hours unit tests) calls now() itself, so snapshots are relative to real now."""
    return (
        (datetime.now(timezone.utc) - timedelta(hours=hours))
        .isoformat()
        .replace("+00:00", "Z")
    )


def test_backup_fresh_is_ok(monkeypatch):
    monkeypatch.setattr(
        check, "BACKUP_PATH", "/data/containers"
    )  # match _sources()'s path
    last = {"endTime": _iso_ago(1), "stats": {"errorCount": 0}}
    monkeypatch.setattr(check, "_get_json", lambda *a, **k: _sources(last))
    ok, msg = check.check_backup()
    assert ok
    assert "0 errors" in msg


def test_backup_errors_alert(monkeypatch):
    monkeypatch.setattr(check, "BACKUP_PATH", "/data/containers")
    last = {"endTime": _iso_ago(1), "stats": {"errorCount": 3}}
    monkeypatch.setattr(check, "_get_json", lambda *a, **k: _sources(last))
    ok, msg = check.check_backup()
    assert not ok
    assert "error" in msg.lower()


def test_backup_stale_alerts(monkeypatch):
    # default BACKUP_MAX_AGE_H=30; a 40h-old snapshot must alert
    monkeypatch.setattr(check, "BACKUP_PATH", "/data/containers")
    last = {"endTime": _iso_ago(40), "stats": {"errorCount": 0}}
    monkeypatch.setattr(check, "_get_json", lambda *a, **k: _sources(last))
    ok, msg = check.check_backup()
    assert not ok
    assert "ago" in msg


def test_backup_missing_source_alerts(monkeypatch):
    # LookupError from backup_age_hours must surface as a descriptive down, not crash
    monkeypatch.setattr(check, "BACKUP_PATH", "/data/containers")
    monkeypatch.setattr(check, "_get_json", lambda *a, **k: {"sources": []})
    ok, msg = check.check_backup()
    assert not ok
    assert "no Kopia source" in msg


# --- check_disk -------------------------------------------------------------


def test_disk_under_threshold_is_ok(monkeypatch):
    monkeypatch.setattr(check, "DISK_MOUNTPOINTS", ["/"])
    # avail 0.5GB of 1GB -> 50% used, under default 90%
    monkeypatch.setattr(check, "prom_scalar", _seq(0.5e9, 1e9))
    ok, msg = check.check_disk()
    assert ok
    assert "under" in msg


def test_disk_over_threshold_names_mount(monkeypatch):
    monkeypatch.setattr(check, "DISK_MOUNTPOINTS", ["/"])
    # avail 0.05GB of 1GB -> 95% used, over default 90%
    monkeypatch.setattr(check, "prom_scalar", _seq(0.05e9, 1e9))
    ok, msg = check.check_disk()
    assert not ok
    assert "/" in msg
    assert "95" in msg


def test_disk_metric_unavailable_alerts(monkeypatch):
    # check_disk binds BOTH avail and size before the None/zero guard -> feed two values
    monkeypatch.setattr(check, "DISK_MOUNTPOINTS", ["/"])
    monkeypatch.setattr(check, "prom_scalar", _seq(None, 1e9))
    ok, msg = check.check_disk()
    assert not ok
    assert "unavailable" in msg


# --- check_cert -------------------------------------------------------------


def test_cert_valid_is_ok(monkeypatch):
    # default CERT_MIN_DAYS=14; 30 days left -> ok
    monkeypatch.setattr(check, "prom_scalar", lambda *a, **k: 30.0)
    ok, msg = check.check_cert()
    assert ok
    assert "valid" in msg


def test_cert_expiring_alerts(monkeypatch):
    # 5 days left < 14 -> down
    monkeypatch.setattr(check, "prom_scalar", lambda *a, **k: 5.0)
    ok, msg = check.check_cert()
    assert not ok
    assert "expires" in msg


def test_cert_metric_unavailable_alerts(monkeypatch):
    monkeypatch.setattr(check, "prom_scalar", lambda *a, **k: None)
    ok, msg = check.check_cert()
    assert not ok
    assert "unavailable" in msg


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
    return {
        "data": [{"workflowId": w, "status": "error", "stoppedAt": s} for w, s in items]
    }


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
        ("2", _n8n_ago(3)),
        ("2", _n8n_ago(4)),
        ("2", _n8n_ago(5)),
    )
    # B has 3 failures, A has 1 -> sorted by count desc
    assert check.n8n_failures(wf, ex, 900, now=N8N_NOW) == [
        ("B Flow", 3),
        ("A Flow", 1),
    ]


def test_n8n_empty_inputs():
    assert check.n8n_failures({"data": []}, {"data": []}, 900, now=N8N_NOW) == []


def test_n8n_missing_stoppedat_falls_back_to_startedat():
    wf = _workflows(("1", "Prod Flow", True))
    ex = {"data": [{"workflowId": "1", "status": "error", "startedAt": _n8n_ago(5)}]}
    assert check.n8n_failures(wf, ex, 900, now=N8N_NOW) == [("Prod Flow", 1)]


def test_n8n_naive_timestamp_treated_as_utc():
    # n8n normally emits UTC 'Z'; a naive timestamp must not raise on the tz-aware compare
    wf = _workflows(("1", "Prod Flow", True))
    naive = (
        (N8N_NOW - timedelta(minutes=5)).replace(tzinfo=None).isoformat()
    )  # no offset/Z
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


# --- check_gitops_alive / check_gitops_status (file I/O) ---------------------


def _gw(tmp_path, name, content):
    (tmp_path / name).write_text(content)


def test_check_gitops_alive_fresh_file(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "GITOPS_STATE_DIR", str(tmp_path))
    _gw(tmp_path, "last_run", str(time.time()))
    ok, _ = check.check_gitops_alive()
    assert ok


def test_check_gitops_alive_stale_file(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "GITOPS_STATE_DIR", str(tmp_path))
    _gw(tmp_path, "last_run", str(time.time() - 100 * 60))  # 100m old > default 90m
    ok, _ = check.check_gitops_alive()
    assert not ok


def test_check_gitops_alive_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "GITOPS_STATE_DIR", str(tmp_path))
    ok, msg = check.check_gitops_alive()
    assert not ok
    assert "no last_run" in msg


def test_check_gitops_alive_unparseable(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "GITOPS_STATE_DIR", str(tmp_path))
    _gw(tmp_path, "last_run", "not-a-float")
    ok, msg = check.check_gitops_alive()
    assert not ok
    assert "unparseable" in msg


def test_check_gitops_status_no_file_is_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "GITOPS_STATE_DIR", str(tmp_path))
    ok, _ = check.check_gitops_status()
    assert ok


def test_check_gitops_status_held(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "GITOPS_STATE_DIR", str(tmp_path))
    _gw(tmp_path, "hold_sha", "abc123def4567890")
    ok, msg = check.check_gitops_status()
    assert not ok
    assert "abc123de" in msg


# ── loop heartbeat (container healthcheck reads this file's mtime) ─────────────


def test_touch_heartbeat_writes_and_refreshes(tmp_path, monkeypatch):
    hb = tmp_path / "heartbeat"
    monkeypatch.setattr(check, "HEARTBEAT_FILE", str(hb))
    check.touch_heartbeat()
    assert hb.exists()
    first = hb.stat().st_mtime
    os.utime(hb, (first - 100, first - 100))  # backdate, then refresh
    check.touch_heartbeat()
    assert hb.stat().st_mtime > first - 100


def test_touch_heartbeat_never_raises(monkeypatch):
    # Best-effort like push(): a heartbeat failure must not kill the loop.
    monkeypatch.setattr(check, "HEARTBEAT_FILE", "/nonexistent-dir/heartbeat")
    check.touch_heartbeat()


# ── kopia restore drill (monthly host cron writes state.json; we alert on it) ──


def _drill_state(tmp_path, monkeypatch, ts, ok, msg):
    p = tmp_path / "state.json"
    p.write_text(
        '{"ts": %s, "ok": %s, "msg": "%s"}' % (ts, "true" if ok else "false", msg)
    )
    monkeypatch.setattr(check, "RESTORE_DRILL_STATE", str(p))


def test_restore_drill_fresh_success_is_up(tmp_path, monkeypatch):
    _drill_state(
        tmp_path, monkeypatch, time.time() - 86400, True, "restored pihole: 23 files"
    )
    ok, msg = check.check_restore_drill()
    assert ok
    assert "pihole" in msg


def test_restore_drill_failure_is_down(tmp_path, monkeypatch):
    _drill_state(tmp_path, monkeypatch, time.time(), False, "restore of n8n failed")
    ok, msg = check.check_restore_drill()
    assert not ok
    assert "n8n" in msg


def test_restore_drill_stale_success_is_down(tmp_path, monkeypatch):
    _drill_state(
        tmp_path, monkeypatch, time.time() - 40 * 86400, True, "restored grafana"
    )
    ok, msg = check.check_restore_drill()
    assert not ok
    assert "ago" in msg


def test_restore_drill_missing_state_is_down(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "RESTORE_DRILL_STATE", str(tmp_path / "nope.json"))
    ok, msg = check.check_restore_drill()
    assert not ok
    assert "never ran" in msg


def test_restore_drill_unparseable_is_down(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    p.write_text("not json")
    monkeypatch.setattr(check, "RESTORE_DRILL_STATE", str(p))
    ok, msg = check.check_restore_drill()
    assert not ok
    assert "unparseable" in msg


# ── weekly kopia snapshot verify (weekly host cron writes state.json; we alert on it) ──


def _verify_state(tmp_path, monkeypatch, ts, ok, msg):
    p = tmp_path / "state.json"
    p.write_text(
        '{"ts": %s, "ok": %s, "msg": "%s"}' % (ts, "true" if ok else "false", msg)
    )
    monkeypatch.setattr(check, "VERIFY_STATE", str(p))


def test_verify_fresh_success_is_up(tmp_path, monkeypatch):
    _verify_state(
        tmp_path,
        monkeypatch,
        time.time() - 86400,
        True,
        "verified 142 snapshots, 0 errors",
    )
    ok, msg = check.check_verify()
    assert ok
    assert "142 snapshots" in msg


def test_verify_failure_is_down(tmp_path, monkeypatch):
    # A non-zero `kopia snapshot verify` (detected bit-rot / unreadable blob) must page —
    # this is the exact failure the old `| logger` cron swallowed.
    _verify_state(
        tmp_path, monkeypatch, time.time(), False, "verify found 2 unreadable objects"
    )
    ok, msg = check.check_verify()
    assert not ok
    assert "unreadable" in msg


def test_verify_stale_success_is_down(tmp_path, monkeypatch):
    # Weekly cadence; a 12d-old success means the verify cron stopped running.
    _verify_state(tmp_path, monkeypatch, time.time() - 12 * 86400, True, "ok")
    ok, msg = check.check_verify()
    assert not ok
    assert "ago" in msg


def test_verify_missing_state_is_down(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "VERIFY_STATE", str(tmp_path / "nope.json"))
    ok, msg = check.check_verify()
    assert not ok
    assert "never ran" in msg


def test_verify_unparseable_is_down(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    p.write_text("not json")
    monkeypatch.setattr(check, "VERIFY_STATE", str(p))
    ok, msg = check.check_verify()
    assert not ok
    assert "unparseable" in msg


def _maintenance_state(tmp_path, monkeypatch, ts, ok, msg):
    p = tmp_path / "maint.json"
    p.write_text(
        '{"ts": %s, "ok": %s, "msg": "%s"}' % (ts, "true" if ok else "false", msg)
    )
    monkeypatch.setattr(check, "MAINTENANCE_STATE", str(p))


def test_maintenance_fresh_success_is_up(tmp_path, monkeypatch):
    _maintenance_state(
        tmp_path,
        monkeypatch,
        time.time() - 3600,
        True,
        "full maint enabled, owner root@kopia, next in 18.0h, last run snapshot-gc ok",
    )
    ok, msg = check.check_maintenance()
    assert ok
    assert "owner root@kopia" in msg


def test_maintenance_unhealthy_is_down(tmp_path, monkeypatch):
    # A stalled/disabled full cycle (the upstream cause b2_usage only catches weeks later) must page.
    _maintenance_state(
        tmp_path, monkeypatch, time.time(), False, "full maintenance overdue 50.0h"
    )
    ok, msg = check.check_maintenance()
    assert not ok
    assert "overdue" in msg


def test_maintenance_stale_success_is_down(tmp_path, monkeypatch):
    # Daily cadence; a 3d-old success means the maintenance-check cron stopped running.
    _maintenance_state(tmp_path, monkeypatch, time.time() - 3 * 86400, True, "ok")
    ok, msg = check.check_maintenance()
    assert not ok
    assert "old" in msg


def test_maintenance_missing_state_is_down(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "MAINTENANCE_STATE", str(tmp_path / "nope.json"))
    ok, msg = check.check_maintenance()
    assert not ok
    assert "never ran" in msg


def test_maintenance_unparseable_is_down(tmp_path, monkeypatch):
    p = tmp_path / "maint.json"
    p.write_text("not json")
    monkeypatch.setattr(check, "MAINTENANCE_STATE", str(p))
    ok, msg = check.check_maintenance()
    assert not ok
    assert "unparseable" in msg


# ── B2 storage usage (daily host cron writes billable bytes; we alert on it) ──


def _b2_state(tmp_path, monkeypatch, ts, ok, bytes_, msg="probe"):
    p = tmp_path / "state.json"
    p.write_text(
        '{"ts": %s, "ok": %s, "bytes": %s, "msg": "%s"}'
        % (ts, "true" if ok else "false", bytes_, msg)
    )
    monkeypatch.setattr(check, "B2_USAGE_STATE", str(p))


def test_b2_usage_under_threshold_is_up(tmp_path, monkeypatch):
    # 6.56GB of the 10GB plan = 66% < the 85% default threshold. Decimal GB
    # throughout — B2 bills decimal, so the message matches its dashboard.
    _b2_state(tmp_path, monkeypatch, time.time() - 3600, True, 6_559_400_355)
    ok, msg = check.check_b2_usage()
    assert ok
    assert "6.56/10GB" in msg


def test_b2_usage_over_threshold_is_down(tmp_path, monkeypatch):
    # 9.5GB of 10GB = 95% > 85% — alert with the threshold in the message.
    _b2_state(tmp_path, monkeypatch, time.time(), True, int(9.5e9))
    ok, msg = check.check_b2_usage()
    assert not ok
    assert "over 85% threshold" in msg


def test_b2_usage_failed_probe_is_down(tmp_path, monkeypatch):
    _b2_state(tmp_path, monkeypatch, time.time(), False, 0, "rclone size query failed")
    ok, msg = check.check_b2_usage()
    assert not ok
    assert "rclone" in msg


def test_b2_usage_stale_state_is_down(tmp_path, monkeypatch):
    # Fresh-enough data is 2.5d; 4d-old state means the daily cron is broken.
    _b2_state(tmp_path, monkeypatch, time.time() - 4 * 86400, True, 1073741824)
    ok, msg = check.check_b2_usage()
    assert not ok
    assert "old" in msg


def test_b2_usage_missing_state_is_down(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "B2_USAGE_STATE", str(tmp_path / "nope.json"))
    ok, msg = check.check_b2_usage()
    assert not ok
    assert "never ran" in msg


def test_b2_usage_invalid_bytes_is_down(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    p.write_text('{"ts": %s, "ok": true, "bytes": null, "msg": "x"}' % time.time())
    monkeypatch.setattr(check, "B2_USAGE_STATE", str(p))
    ok, msg = check.check_b2_usage()
    assert not ok
    assert "bytes" in msg


# ── scrutiny SMART-data freshness (collector runs daily; web API holds last report) ──


def _summary(*entries):
    return {e["device"]["wwn"]: e for e in entries}


def _dev(wwn, name, collector_date=None, archived=False):
    e = {"device": {"wwn": wwn, "device_name": name, "archived": archived}}
    e["smart"] = {"collector_date": collector_date} if collector_date else None
    return e


def test_scrutiny_fresh_device_is_ok():
    s = _summary(_dev("w1", "nvme0", "2026-06-06T06:00:00Z"))
    ok, msg = check.scrutiny_freshness(
        s, 26, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)
    )
    assert ok
    assert "1 device" in msg


def test_scrutiny_stale_device_is_named():
    s = _summary(
        _dev("w1", "nvme0", "2026-06-04T06:00:00Z"),
        _dev("w2", "sda", "2026-06-06T06:00:00Z"),
    )
    ok, msg = check.scrutiny_freshness(
        s, 26, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)
    )
    assert not ok
    assert "nvme0" in msg and "sda" not in msg


def test_scrutiny_no_smart_data_is_down():
    s = _summary(_dev("w1", "nvme0"))
    ok, msg = check.scrutiny_freshness(
        s, 26, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)
    )
    assert not ok
    assert "no SMART data" in msg


def test_scrutiny_archived_device_is_skipped():
    s = _summary(
        _dev("w1", "nvme0", "2026-06-06T06:00:00Z"),
        _dev("w2", "old-disk", "2020-01-01T00:00:00Z", archived=True),
    )
    ok, _ = check.scrutiny_freshness(
        s, 26, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)
    )
    assert ok


def test_scrutiny_no_devices_is_down():
    ok, msg = check.scrutiny_freshness({}, 26)
    assert not ok
    assert "no devices" in msg


# ── pi_pressure (Pi load / memory / disk headroom via the Pi's glances API) ──


MB = 1048576

LOAD_OK = {"min5": 0.8, "cpucore": 4}
MEM_OK = {"available": 150 * MB}
# Glances in its container sees its own bind-mounts (/etc/resolv.conf etc.), all backed
# by the SD card device with the HOST fs usage percent — so entries are keyed by
# device_name, and one device appears many times.
FS_OK = [
    {"device_name": "/dev/mmcblk0p2", "mnt_point": "/etc/resolv.conf", "percent": 3.3},
    {"device_name": "/dev/mmcblk0p2", "mnt_point": "/etc/hostname", "percent": 3.3},
]


def test_pi_pressure_ok():
    ok, msg = check.pi_pressure(LOAD_OK, MEM_OK, FS_OK, 1.5, 50, 90)
    assert ok
    assert "0.20/core" in msg and "150MB" in msg and "disk 3%" in msg


def test_pi_pressure_high_load_alerts():
    # 2026-06-11 fwupd incident signature: load5 ~7.2 on 4 cores while every
    # container healthcheck timed out (mem available still ~150MB at that instant)
    ok, msg = check.pi_pressure({"min5": 7.2, "cpucore": 4}, MEM_OK, FS_OK, 1.5, 50, 90)
    assert not ok
    assert "load5 1.80/core" in msg


def test_pi_pressure_low_mem_alerts():
    ok, msg = check.pi_pressure(
        {"min5": 0.4, "cpucore": 4}, {"available": 13 * MB}, FS_OK, 1.5, 50, 90
    )
    assert not ok
    assert "13MB" in msg


def test_pi_pressure_full_disk_alerts_naming_device():
    fs = [
        {"device_name": "/dev/mmcblk0p2", "mnt_point": "/etc/hostname", "percent": 94.0}
    ]
    ok, msg = check.pi_pressure(LOAD_OK, MEM_OK, fs, 1.5, 50, 90)
    assert not ok
    assert "/dev/mmcblk0p2" in msg and "94" in msg


def test_pi_pressure_duplicate_device_entries_alert_once():
    fs = [
        {
            "device_name": "/dev/mmcblk0p2",
            "mnt_point": "/etc/resolv.conf",
            "percent": 94.0,
        },
        {
            "device_name": "/dev/mmcblk0p2",
            "mnt_point": "/etc/hostname",
            "percent": 94.0,
        },
    ]
    ok, msg = check.pi_pressure(LOAD_OK, MEM_OK, fs, 1.5, 50, 90)
    assert not ok
    assert msg.count("/dev/mmcblk0p2") == 1


def test_pi_pressure_both_breaches_named():
    ok, msg = check.pi_pressure(
        {"min5": 8.0, "cpucore": 4}, {"available": 10 * MB}, FS_OK, 1.5, 50, 90
    )
    assert not ok
    assert "load5" in msg and "available" in msg


def test_pi_pressure_at_threshold_is_ok():
    # strictly greater / strictly less, like the other checks' threshold semantics
    fs = [{"device_name": "/dev/mmcblk0p2", "mnt_point": "/", "percent": 90.0}]
    ok, _ = check.pi_pressure(
        {"min5": 6.0, "cpucore": 4}, {"available": 50 * MB}, fs, 1.5, 50, 90
    )
    assert ok


def test_pi_pressure_missing_fields_alert():
    ok, msg = check.pi_pressure({}, MEM_OK, FS_OK, 1.5, 50, 90)
    assert not ok
    assert "missing" in msg


def test_pi_pressure_empty_fs_alerts():
    # a glances fs-plugin regression must surface, not silently pass (same principle
    # as the load/mem missing-field handling)
    ok, msg = check.pi_pressure(LOAD_OK, MEM_OK, [], 1.5, 50, 90)
    assert not ok
    assert "missing" in msg


def test_pi_pressure_zero_cores_alerts_not_divides():
    ok, msg = check.pi_pressure({"min5": 1.0, "cpucore": 0}, MEM_OK, FS_OK, 1.5, 50, 90)
    assert not ok
    assert "missing" in msg


# --- check_pi_pressure -------------------------------------------------------


def test_pi_check_disabled_without_url():
    # PI_GLANCES_URL defaults to "" in tests -> monitoring disabled, never a false page
    ok, msg = check.check_pi_pressure()
    assert ok
    assert "disabled" in msg.lower()


def test_pi_check_down_on_pressure(monkeypatch):
    monkeypatch.setattr(check, "PI_GLANCES_URL", "http://pi:61208")
    monkeypatch.setattr(
        check, "_get_json", _seq({"min5": 7.2, "cpucore": 4}, MEM_OK, FS_OK)
    )
    ok, msg = check.check_pi_pressure()
    assert not ok
    assert "load5" in msg


def test_pi_check_up_when_quiet(monkeypatch):
    monkeypatch.setattr(check, "PI_GLANCES_URL", "http://pi:61208")
    monkeypatch.setattr(
        check, "_get_json", _seq({"min5": 0.4, "cpucore": 4}, MEM_OK, FS_OK)
    )
    ok, _ = check.check_pi_pressure()
    assert ok


# ── HA automation-engine heartbeat (input_datetime stamped by a 1-min automation) ──
# ha_heartbeat_fresh reads last_changed off the /api/states/input_datetime.ha_heartbeat
# payload: fresh => the scheduler ran recently; stale/missing => wedged or never ran.
HB_NOW = datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc)


def _ha_state(last_changed, state="2026-06-06 11:59:00"):
    """Minimal HA state shape — only last_changed is read by the check."""
    return {
        "entity_id": "input_datetime.ha_heartbeat",
        "state": state,
        "last_changed": last_changed,
        "last_updated": last_changed,
    }


def test_ha_heartbeat_fresh_is_ok():
    ok, msg = check.ha_heartbeat_fresh(
        _ha_state("2026-06-06T11:59:00Z"), 300, now=HB_NOW
    )  # 60s old
    assert ok
    assert "fresh" in msg


def test_ha_heartbeat_stale_is_down():
    ok, msg = check.ha_heartbeat_fresh(
        _ha_state("2026-06-06T11:50:00Z"), 300, now=HB_NOW
    )  # 600s old
    assert not ok
    assert "stale" in msg


def test_ha_heartbeat_at_threshold_is_ok():
    ok, _ = check.ha_heartbeat_fresh(
        _ha_state("2026-06-06T11:55:00Z"), 300, now=HB_NOW
    )  # exactly 300s
    assert ok


def test_ha_heartbeat_missing_last_changed_is_down():
    ok, _ = check.ha_heartbeat_fresh({"state": "unknown"}, 300, now=HB_NOW)
    assert not ok


def test_ha_heartbeat_none_state_is_down():
    ok, _ = check.ha_heartbeat_fresh(None, 300, now=HB_NOW)
    assert not ok


# ── check_ha_heartbeat hysteresis (rides out the ~120s deploy/restart) ──────
# A redeploy makes the HTTP API briefly unreachable AND leaves the automation
# scheduler a beat behind, so a single cycle can read unreachable OR stale. Like
# CPU_CONSECUTIVE, only HA_CONSECUTIVE straight down-cycles page; a single blip
# pushes up with a streak msg. ha_heartbeat_fresh uses the real clock (no `now`
# override on this path), so payloads are built relative to real now.
def _ha_payload(age_s):
    lc = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()
    return _ha_state(lc)


def _ha_cycle(monkeypatch, age_s=600, raises=False):
    monkeypatch.setattr(check, "HA_URL", "http://home-assistant:8123")
    monkeypatch.setattr(check, "HA_TOKEN", "tok")
    if raises:

        def boom(*a, **k):
            raise OSError("connection refused")

        monkeypatch.setattr(check, "_get_json", boom)
    else:
        monkeypatch.setattr(check, "_get_json", lambda *a, **k: _ha_payload(age_s))
    return check.check_ha_heartbeat()


def test_ha_heartbeat_single_stale_cycle_is_suppressed(monkeypatch):
    # One stale cycle (a deploy mid-recreate) must NOT page — pushes up with a streak msg.
    monkeypatch.setattr(check, "_ha_down_streak", 0)
    ok, msg = _ha_cycle(monkeypatch, age_s=600)
    assert ok
    assert "1/2" in msg  # streak progress vs default HA_CONSECUTIVE=2


def test_ha_heartbeat_two_consecutive_stale_cycles_alert(monkeypatch):
    # Default HA_CONSECUTIVE=2: the 2nd straight stale cycle is a genuinely wedged HA -> down.
    monkeypatch.setattr(check, "_ha_down_streak", 0)
    ok, _ = _ha_cycle(monkeypatch, age_s=600)
    assert ok
    ok, msg = _ha_cycle(monkeypatch, age_s=600)
    assert not ok
    assert "stale" in msg


def test_ha_heartbeat_fresh_read_resets_streak(monkeypatch):
    # stale, then fresh -> never down (a recovered deploy clears the streak).
    monkeypatch.setattr(check, "_ha_down_streak", 0)
    assert _ha_cycle(monkeypatch, age_s=600)[0]
    ok, msg = _ha_cycle(monkeypatch, age_s=60)  # scheduler resumed, heartbeat fresh
    assert ok
    assert "fresh" in msg
    # the next stale cycle starts a NEW streak, so it's suppressed again
    ok, msg = _ha_cycle(monkeypatch, age_s=600)
    assert ok
    assert "1/2" in msg


def test_ha_heartbeat_unreachable_api_rides_grace(monkeypatch):
    # The recreate-window connection error must ride the SAME grace, not page immediately.
    monkeypatch.setattr(check, "_ha_down_streak", 0)
    ok, msg = _ha_cycle(monkeypatch, raises=True)
    assert ok
    assert "1/2" in msg


def test_ha_heartbeat_disabled_when_no_url_token(monkeypatch):
    monkeypatch.setattr(check, "HA_URL", "")
    monkeypatch.setattr(check, "HA_TOKEN", "")
    ok, msg = check.check_ha_heartbeat()
    assert ok
    assert "disabled" in msg


# --- renovate_alive / check_renovate_alive ---------------------------------


def test_renovate_alive_fresh():
    ok, msg = check.renovate_alive(60, 129600)  # 36h = 129600s
    assert ok
    assert "1m ago" in msg


def test_renovate_alive_at_threshold_is_ok():
    ok, _ = check.renovate_alive(129600, 129600)
    assert ok


def test_renovate_alive_stale():
    ok, msg = check.renovate_alive(140000, 129600)
    assert not ok
    assert "ago" in msg


def test_check_renovate_alive_missing_marker_is_down(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "RENOVATE_STATE_DIR", str(tmp_path))
    ok, msg = check.check_renovate_alive()
    assert not ok
    assert "no last_run marker" in msg


def test_check_renovate_alive_fresh_file_is_up(tmp_path, monkeypatch):
    import time as _t

    monkeypatch.setattr(check, "RENOVATE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(check, "RENOVATE_MAX_AGE_S", 129600)
    (tmp_path / "last_run").write_text(str(_t.time()))
    ok, _ = check.check_renovate_alive()
    assert ok


# --- loki ingestion freshness -----------------------------------------------
# Loki's Kuma /ready probe stays green even if promtail stops shipping (DOCKER_HOST
# break, positions-file corruption, label regression) — a silently-dead log pipeline.
# This check counts ingested log lines for an always-active stream over a window and
# goes down when zero: a freshness watchdog analogous to the SMART/restore-drill ones.


def _loki_scalar(val):
    """A Loki instant-query response for `sum(count_over_time(...))`. None -> empty result."""
    if val is None:
        return {"status": "success", "data": {"resultType": "vector", "result": []}}
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": {}, "value": [1700000000, str(val)]}],
        },
    }


def test_loki_ingestion_with_lines_is_ok():
    ok, msg = check.loki_ingestion_fresh(1234, "10m")
    assert ok
    assert "1234" in msg


def test_loki_ingestion_zero_lines_is_down():
    ok, msg = check.loki_ingestion_fresh(0, "10m")
    assert not ok
    assert "silent" in msg


def test_loki_ingestion_no_series_is_down():
    # an empty query result (no matching stream at all) is also a silent pipeline
    ok, msg = check.loki_ingestion_fresh(None, "10m")
    assert not ok


def test_loki_count_parses_value(monkeypatch):
    monkeypatch.setattr(check, "_get_json", lambda *a, **k: _loki_scalar(42))
    assert check.loki_count('{job="syslog"}', "10m") == 42.0


def test_loki_count_empty_result_is_none(monkeypatch):
    monkeypatch.setattr(check, "_get_json", lambda *a, **k: _loki_scalar(None))
    assert check.loki_count('{job="syslog"}', "10m") is None


def test_recyclarr_sync_succeeded_is_ok():
    ok, msg = check.recyclarr_sync_ok(0, 1, "26h")
    assert ok
    assert "1 succeeded" in msg


def test_recyclarr_sync_failed_is_down():
    # A `job failed` line means recyclarr exited non-zero — down even if an earlier run succeeded.
    ok, msg = check.recyclarr_sync_ok(1, 1, "26h")
    assert not ok
    assert "failed" in msg


def test_recyclarr_no_successful_run_is_down():
    # No `job succeeded` in the window: the supercronic scheduler stalled (the healthcheck only
    # proves supercronic is alive, not that the sync ran) — a silent gap the old setup couldn't see.
    ok, msg = check.recyclarr_sync_ok(0, 0, "26h")
    assert not ok
    assert "no successful" in msg


def test_recyclarr_none_counts_treated_as_zero_is_down():
    # No matching Loki series (recyclarr not shipping, or never ran) -> None -> 0 -> down.
    ok, _ = check.recyclarr_sync_ok(None, None, "26h")
    assert not ok


def test_loki_count_non_success_raises(monkeypatch):
    monkeypatch.setattr(check, "_get_json", lambda *a, **k: {"status": "error"})
    with pytest.raises(RuntimeError):
        check.loki_count('{job="syslog"}', "10m")


def test_check_loki_ingestion_fresh_is_up(monkeypatch):
    monkeypatch.setattr(check, "loki_count", lambda *a, **k: 500)
    ok, _ = check.check_loki_ingestion()
    assert ok


def test_check_loki_ingestion_silent_is_down(monkeypatch):
    monkeypatch.setattr(check, "loki_count", lambda *a, **k: 0)
    ok, msg = check.check_loki_ingestion()
    assert not ok


def test_check_loki_ingestion_docker_stream_silent_is_down(monkeypatch):
    # docker_sd-specific failure: the static file-tail union ({job=~".+"}) keeps flowing,
    # but the highest-volume container-log stream ({container=~".+"}) went silent. The
    # union count alone stays non-zero and would hide it — the docker-specific arm must page.
    def fake_count(selector, window):
        return 0 if "container" in selector else 500

    monkeypatch.setattr(check, "loki_count", fake_count)
    ok, msg = check.check_loki_ingestion()
    assert not ok
    assert "container" in msg


# --- discord_webhook_ok / check_discord -------------------------------------


def test_discord_webhook_ok_200_is_up():
    ok, msg = check.discord_webhook_ok(200, "Homelab Alerts")
    assert ok
    assert "Homelab Alerts" in msg


def test_discord_webhook_404_is_down():
    ok, msg = check.discord_webhook_ok(404)
    assert not ok
    assert "404" in msg


def _discord_cycle(monkeypatch, status=200, raises=None):
    monkeypatch.setattr(
        check, "DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/abc"
    )
    if raises is not None:

        def boom(*a, **k):
            raise raises

        monkeypatch.setattr(check, "_get_json", boom)
    elif status == 200:
        monkeypatch.setattr(
            check, "_get_json", lambda *a, **k: {"name": "Homelab Alerts"}
        )
    else:

        def http_err(*a, **k):
            raise urllib.error.HTTPError("u", status, "err", {}, None)

        monkeypatch.setattr(check, "_get_json", http_err)
    return check.check_discord()


def test_discord_single_failure_is_suppressed(monkeypatch):
    # One non-200 (a transient blip on the internet-facing check) must NOT page.
    monkeypatch.setattr(check, "_discord_down_streak", 0)
    ok, msg = _discord_cycle(monkeypatch, status=404)
    assert ok
    assert "1/2" in msg


def test_discord_two_consecutive_failures_alert(monkeypatch):
    # The 2nd straight failure is a genuinely dead webhook -> down.
    monkeypatch.setattr(check, "_discord_down_streak", 0)
    assert _discord_cycle(monkeypatch, status=404)[0]
    ok, msg = _discord_cycle(monkeypatch, status=404)
    assert not ok
    assert "404" in msg


def test_discord_valid_read_resets_streak(monkeypatch):
    monkeypatch.setattr(check, "_discord_down_streak", 0)
    assert _discord_cycle(monkeypatch, status=404)[0]  # streak 1
    ok, msg = _discord_cycle(monkeypatch, status=200)  # webhook recovered
    assert ok
    assert "valid" in msg
    ok, msg = _discord_cycle(monkeypatch, status=404)  # new streak, suppressed again
    assert ok
    assert "1/2" in msg


def test_discord_unreachable_rides_grace(monkeypatch):
    monkeypatch.setattr(check, "_discord_down_streak", 0)
    ok, msg = _discord_cycle(monkeypatch, raises=OSError("dns fail"))
    assert ok
    assert "1/2" in msg


def test_discord_disabled_without_url(monkeypatch):
    monkeypatch.setattr(check, "DISCORD_WEBHOOK_URL", "")
    ok, msg = check.check_discord()
    assert ok
    assert "disabled" in msg
