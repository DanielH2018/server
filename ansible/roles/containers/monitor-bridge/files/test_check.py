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


# --- check_backup -----------------------------------------------------------

def _iso_ago(hours):
    """RFC3339 'Z' timestamp `hours` in the past. check_backup() (unlike the
    backup_age_hours unit tests) calls now() itself, so snapshots are relative to real now."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def test_backup_fresh_is_ok(monkeypatch):
    monkeypatch.setattr(check, "BACKUP_PATH", "/data/containers")  # match _sources()'s path
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
    p.write_text('{"ts": %s, "ok": %s, "msg": "%s"}' % (ts, "true" if ok else "false", msg))
    monkeypatch.setattr(check, "RESTORE_DRILL_STATE", str(p))


def test_restore_drill_fresh_success_is_up(tmp_path, monkeypatch):
    _drill_state(tmp_path, monkeypatch, time.time() - 86400, True, "restored pihole: 23 files")
    ok, msg = check.check_restore_drill()
    assert ok
    assert "pihole" in msg


def test_restore_drill_failure_is_down(tmp_path, monkeypatch):
    _drill_state(tmp_path, monkeypatch, time.time(), False, "restore of n8n failed")
    ok, msg = check.check_restore_drill()
    assert not ok
    assert "n8n" in msg


def test_restore_drill_stale_success_is_down(tmp_path, monkeypatch):
    _drill_state(tmp_path, monkeypatch, time.time() - 40 * 86400, True, "restored grafana")
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


# ── B2 storage usage (daily host cron writes billable bytes; we alert on it) ──


def _b2_state(tmp_path, monkeypatch, ts, ok, bytes_, msg="probe"):
    p = tmp_path / "state.json"
    p.write_text('{"ts": %s, "ok": %s, "bytes": %s, "msg": "%s"}'
                 % (ts, "true" if ok else "false", bytes_, msg))
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
    ok, msg = check.scrutiny_freshness(s, 26, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    assert ok
    assert "1 device" in msg


def test_scrutiny_stale_device_is_named():
    s = _summary(_dev("w1", "nvme0", "2026-06-04T06:00:00Z"),
                 _dev("w2", "sda", "2026-06-06T06:00:00Z"))
    ok, msg = check.scrutiny_freshness(s, 26, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    assert not ok
    assert "nvme0" in msg and "sda" not in msg


def test_scrutiny_no_smart_data_is_down():
    s = _summary(_dev("w1", "nvme0"))
    ok, msg = check.scrutiny_freshness(s, 26, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    assert not ok
    assert "no SMART data" in msg


def test_scrutiny_archived_device_is_skipped():
    s = _summary(_dev("w1", "nvme0", "2026-06-06T06:00:00Z"),
                 _dev("w2", "old-disk", "2020-01-01T00:00:00Z", archived=True))
    ok, _ = check.scrutiny_freshness(s, 26, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    assert ok


def test_scrutiny_no_devices_is_down():
    ok, msg = check.scrutiny_freshness({}, 26)
    assert not ok
    assert "no devices" in msg


# ── pi_pressure (Pi load / memory headroom via the Pi's glances API) ─────────


MB = 1048576


def test_pi_pressure_ok():
    ok, msg = check.pi_pressure({"min5": 0.8, "cpucore": 4}, {"available": 150 * MB}, 1.5, 50)
    assert ok
    assert "0.20/core" in msg and "150MB" in msg


def test_pi_pressure_high_load_alerts():
    # 2026-06-11 fwupd incident signature: load5 ~7.2 on 4 cores while every
    # container healthcheck timed out (mem available still ~150MB at that instant)
    ok, msg = check.pi_pressure({"min5": 7.2, "cpucore": 4}, {"available": 150 * MB}, 1.5, 50)
    assert not ok
    assert "load5 1.80/core" in msg


def test_pi_pressure_low_mem_alerts():
    ok, msg = check.pi_pressure({"min5": 0.4, "cpucore": 4}, {"available": 13 * MB}, 1.5, 50)
    assert not ok
    assert "13MB" in msg


def test_pi_pressure_both_breaches_named():
    ok, msg = check.pi_pressure({"min5": 8.0, "cpucore": 4}, {"available": 10 * MB}, 1.5, 50)
    assert not ok
    assert "load5" in msg and "available" in msg


def test_pi_pressure_at_threshold_is_ok():
    # strictly greater / strictly less, like the other checks' threshold semantics
    ok, _ = check.pi_pressure({"min5": 6.0, "cpucore": 4}, {"available": 50 * MB}, 1.5, 50)
    assert ok


def test_pi_pressure_missing_fields_alert():
    ok, msg = check.pi_pressure({}, {"available": 150 * MB}, 1.5, 50)
    assert not ok
    assert "missing" in msg


def test_pi_pressure_zero_cores_alerts_not_divides():
    ok, msg = check.pi_pressure({"min5": 1.0, "cpucore": 0}, {"available": 150 * MB}, 1.5, 50)
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
        check, "_get_json", _seq({"min5": 7.2, "cpucore": 4}, {"available": 150 * MB}))
    ok, msg = check.check_pi_pressure()
    assert not ok
    assert "load5" in msg


def test_pi_check_up_when_quiet(monkeypatch):
    monkeypatch.setattr(check, "PI_GLANCES_URL", "http://pi:61208")
    monkeypatch.setattr(
        check, "_get_json", _seq({"min5": 0.4, "cpucore": 4}, {"available": 150 * MB}))
    ok, _ = check.check_pi_pressure()
    assert ok
