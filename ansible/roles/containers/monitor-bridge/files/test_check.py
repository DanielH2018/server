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


# --- _env_file --------------------------------------------------------------
def test_env_file_reads_from_file_and_strips(monkeypatch, tmp_path):
    f = tmp_path / "secret"
    # trailing newline from a rendered file must be stripped
    f.write_text("s3cret-token\n")
    monkeypatch.setenv("HA_TOKEN_FILE", str(f))
    monkeypatch.setenv("HA_TOKEN", "inline-should-be-ignored")
    assert check._env_file("HA_TOKEN", "") == "s3cret-token"


def test_env_file_falls_back_to_plain_env(monkeypatch):
    monkeypatch.delenv("HA_TOKEN_FILE", raising=False)
    monkeypatch.setenv("HA_TOKEN", "inline-token")
    assert check._env_file("HA_TOKEN", "") == "inline-token"


def test_env_file_default_when_neither_set(monkeypatch):
    monkeypatch.delenv("HA_TOKEN_FILE", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    assert check._env_file("HA_TOKEN", "") == ""


def test_env_file_missing_file_falls_back_to_env(monkeypatch, tmp_path):
    # A *_FILE path that doesn't exist must degrade to the plain env var, not raise — _env_file runs
    # at import for HA_TOKEN, so an unguarded open() would crash the whole loop and silence every
    # monitor over one missing file (2026-07-15 review L1).
    monkeypatch.setenv("HA_TOKEN_FILE", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("HA_TOKEN", "inline-fallback")
    assert check._env_file("HA_TOKEN", "") == "inline-fallback"


def test_env_file_directory_path_falls_back_to_env(monkeypatch, tmp_path):
    # The specific Docker failure mode: an absent bind-mount source is created as a directory, so
    # open() raises IsADirectoryError (an OSError subclass) — must still fall back to the env var.
    monkeypatch.setenv("HA_TOKEN_FILE", str(tmp_path))  # tmp_path is a directory
    monkeypatch.setenv("HA_TOKEN", "inline-fallback")
    assert check._env_file("HA_TOKEN", "") == "inline-fallback"


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


# --- backup_source / backup_size_regression ---------------------------------


def _snaps(*pairs):
    """Kopia /api/v1/snapshots list from (files, size) pairs, oldest-first (endTime ascending)."""
    return [
        {
            "endTime": "2026-06-%02dT00:00:00Z" % (i + 1),
            "summary": {"files": f, "size": sz},
        }
        for i, (f, sz) in enumerate(pairs)
    ]


def test_backup_source_returns_identity():
    src = check.backup_source(_sources({"endTime": "x"}), "/data/containers")
    assert src["path"] == "/data/containers"


def test_backup_source_missing_raises():
    with pytest.raises(LookupError):
        check.backup_source({"sources": []}, "/data/containers")


def test_size_regression_steady_growth_is_ok():
    snaps = _snaps((5000, 9e8), (6000, 1.0e9), (7000, 1.1e9), (8000, 1.2e9))
    ok, msg = check.backup_size_regression(snaps, 20, 3)
    assert ok
    assert "size ok" in msg


def test_size_regression_file_count_drop_alerts():
    # latest file count collapses far below the trailing median -> a service left backup scope
    snaps = _snaps((8000, 1.2e9), (8100, 1.2e9), (8050, 1.2e9), (3000, 1.19e9))
    ok, msg = check.backup_size_regression(snaps, 20, 3)
    assert not ok
    assert "file count" in msg and "left backup scope" in msg


def test_size_regression_byte_drop_alerts():
    # files steady but total bytes collapse (a large bind mount vanished) -> down
    snaps = _snaps((8000, 1.2e9), (8000, 1.2e9), (8000, 1.2e9), (7900, 0.5e9))
    ok, msg = check.backup_size_regression(snaps, 20, 3)
    assert not ok
    assert "size dropped" in msg


def test_size_regression_median_absorbs_one_off_dip():
    # a single prior exclusion-tuning dip must not drag the median enough to flag a healthy latest
    snaps = _snaps((8000, 1.2e9), (6500, 1.0e9), (8000, 1.2e9), (8000, 1.2e9))
    ok, _ = check.backup_size_regression(snaps, 20, 3)
    assert ok


def test_size_regression_insufficient_history_is_ok():
    ok, msg = check.backup_size_regression(_snaps((5000, 9e8), (8000, 1.2e9)), 20, 3)
    assert ok
    assert "history" in msg


def test_check_backup_shrink_folds_into_down(monkeypatch):
    # freshness + errorCount clean, but the latest snapshot shrank -> check_backup goes down
    monkeypatch.setattr(check, "BACKUP_PATH", "/data/containers")
    last = {"endTime": _iso_ago(1), "stats": {"errorCount": 0}}
    shrunk = {
        "snapshots": _snaps((8000, 1.2e9), (8000, 1.2e9), (8000, 1.2e9), (3000, 1.2e9))
    }
    monkeypatch.setattr(check, "_get_json", _seq(_sources(last), shrunk))
    ok, msg = check.check_backup()
    assert not ok
    assert "left backup scope" in msg


# --- backup_presence_regression ---------------------------------------------


def _listing(*names_with_files):
    """A snapshot's {top_level_dir_name: recursive_file_count} listing from (name, files) pairs."""
    return dict(names_with_files)


def test_presence_stable_set_is_ok():
    full = _listing(("authelia", 5), ("portainer", 3), ("grafana", 9))
    ok, msg = check.backup_presence_regression([full, full, full, full], 3)
    assert ok
    assert "expected" in msg


def test_presence_vanished_dir_alerts():
    full = _listing(("authelia", 5), ("portainer", 3), ("grafana", 9))
    latest = _listing(("authelia", 5), ("grafana", 9))  # portainer gone
    ok, msg = check.backup_presence_regression([full, full, full, latest], 3)
    assert not ok
    assert "portainer" in msg and "vanished" in msg


def test_presence_zero_files_counts_as_vanished():
    full = _listing(("authelia", 5), ("portainer", 3))
    latest = _listing(("authelia", 5), ("portainer", 0))  # dir present but emptied
    ok, msg = check.backup_presence_regression([full, full, full, latest], 3)
    assert not ok
    assert "portainer" in msg


def test_presence_newly_added_dir_is_not_flagged():
    prior = _listing(("authelia", 5))
    latest = _listing(("authelia", 5), ("newsvc", 2))  # added, absent from priors
    ok, _ = check.backup_presence_regression([prior, prior, prior, latest], 3)
    assert ok


def test_presence_intentional_removal_clears_after_one_cycle():
    # once the removal is itself in a prior snapshot, the dir is no longer "expected" -> no page
    full = _listing(("authelia", 5), ("portainer", 3))
    removed = _listing(("authelia", 5))
    ok, _ = check.backup_presence_regression([full, full, removed, removed], 3)
    assert ok


def test_presence_insufficient_history_is_ok():
    full = _listing(("authelia", 5))
    ok, msg = check.backup_presence_regression([full, full], 3)
    assert ok
    assert "history" in msg


def test_presence_empty_latest_is_skipped():
    full = _listing(("authelia", 5))
    ok, msg = check.backup_presence_regression([full, full, full, {}], 3)
    assert ok
    assert "empty" in msg


def _objlisting(names):
    return {"entries": [{"name": n, "type": "d", "summ": {"files": 5}} for n in names]}


def test_check_backup_presence_folds_into_down(monkeypatch):
    # freshness + size clean, but a service dir in all prior snapshots vanished from the latest ->
    # check_backup goes down via the presence guard (exercises the /api/v1/objects fetch path)
    monkeypatch.setattr(check, "BACKUP_PATH", "/data/containers")
    last = {"endTime": _iso_ago(1), "stats": {"errorCount": 0}}
    steady = _snaps((8000, 1.2e9), (8000, 1.2e9), (8000, 1.2e9), (8000, 1.2e9))
    for i, s in enumerate(steady):
        s["rootID"] = "r%d" % i
    full = ["authelia", "portainer", "grafana"]
    objs = [
        _objlisting(full),
        _objlisting(full),
        _objlisting(full),
        _objlisting(["authelia", "grafana"]),  # portainer dropped in the latest
    ]
    monkeypatch.setattr(
        check, "_get_json", _seq(_sources(last), {"snapshots": steady}, *objs)
    )
    ok, msg = check.check_backup()
    assert not ok
    assert "portainer" in msg and "vanished" in msg


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


# --- queue_warnings (pure) ---------------------------------------------------


def _queue(*records):
    return {"records": list(records)}


def test_queue_warnings_flags_warning_status():
    # The 2026-07-01 incident shape: warning status, importPending state, a statusMessage
    # naming the executable.
    q = _queue(
        {
            "title": "Poisoned.Episode.S01E01.exe",
            "trackedDownloadStatus": "warning",
            "trackedDownloadState": "importPending",
            "statusMessages": [
                {
                    "title": "Poisoned.Episode.S01E01.exe",
                    "messages": [
                        "Caution: Found executable file with extension: '.exe'"
                    ],
                }
            ],
        }
    )
    offenders = check.queue_warnings(q, "Sonarr")
    assert len(offenders) == 1
    app, title, reason = offenders[0]
    assert app == "Sonarr"
    assert title == "Poisoned.Episode.S01E01.exe"
    assert "executable" in reason


def test_queue_warnings_empty_queue_is_clean():
    assert check.queue_warnings(_queue(), "Radarr") == []


def test_queue_warnings_ignores_normal_downloading_item():
    q = _queue(
        {
            "title": "Some.Movie.2026",
            "trackedDownloadStatus": "ok",
            "trackedDownloadState": "downloading",
        }
    )
    assert check.queue_warnings(q, "Radarr") == []


def test_queue_warnings_flags_import_blocked_state():
    q = _queue(
        {
            "title": "Blocked.Release",
            "trackedDownloadStatus": "ok",
            "trackedDownloadState": "importBlocked",
        }
    )
    offenders = check.queue_warnings(q, "Sonarr")
    assert len(offenders) == 1
    assert offenders[0][1] == "Blocked.Release"


def test_queue_warnings_flags_error_status():
    # Upstream trackedDownloadStatus enum is ok/warning/error — "error" is at least as
    # actionable as "warning" and was previously skipped (2026-07-02 review L2).
    q = _queue(
        {
            "title": "Errored.Release",
            "trackedDownloadStatus": "error",
            "trackedDownloadState": "downloading",
        }
    )
    offenders = check.queue_warnings(q, "Radarr")
    assert len(offenders) == 1
    assert offenders[0][1] == "Errored.Release"
    assert offenders[0][2] == "error"


def test_queue_warnings_flags_import_failed_state():
    q = _queue(
        {
            "title": "Failed.Import",
            "trackedDownloadStatus": "ok",
            "trackedDownloadState": "importFailed",
        }
    )
    offenders = check.queue_warnings(q, "Sonarr")
    assert len(offenders) == 1
    assert offenders[0][1] == "Failed.Import"


def test_queue_warnings_import_pending_without_messages_is_ok():
    # Ordinary just-finished-downloading queue item — not a problem.
    q = _queue(
        {
            "title": "Fine.Release",
            "trackedDownloadStatus": "ok",
            "trackedDownloadState": "importPending",
        }
    )
    assert check.queue_warnings(q, "Sonarr") == []


def test_queue_warnings_import_pending_with_messages_is_flagged():
    q = _queue(
        {
            "title": "Ambiguous.Release",
            "trackedDownloadStatus": "ok",
            "trackedDownloadState": "importPending",
            "statusMessages": [{"title": "x", "messages": ["Not a valid video file"]}],
        }
    )
    offenders = check.queue_warnings(q, "Radarr")
    assert len(offenders) == 1
    assert "Not a valid video file" in offenders[0][2]


def test_queue_warnings_multiple_records_all_named():
    q = _queue(
        {
            "title": "Bad One",
            "trackedDownloadStatus": "warning",
            "trackedDownloadState": "importPending",
        },
        {
            "title": "Good One",
            "trackedDownloadStatus": "ok",
            "trackedDownloadState": "downloading",
        },
        {
            "title": "Bad Two",
            "trackedDownloadStatus": "warning",
            "trackedDownloadState": "importPending",
        },
    )
    offenders = check.queue_warnings(q, "Sonarr")
    titles = {t for _, t, _ in offenders}
    assert titles == {"Bad One", "Bad Two"}


# --- check_arr_queue ---------------------------------------------------------


def test_arr_queue_disabled_without_keys():
    # SONARR_API_KEY/RADARR_API_KEY default to "" in tests -> monitoring disabled
    ok, msg = check.check_arr_queue()
    assert ok
    assert "disabled" in msg.lower()


def test_arr_queue_down_on_sonarr_warning(monkeypatch):
    monkeypatch.setattr(check, "SONARR_API_KEY", "x")
    q = _queue(
        {
            "title": "Poisoned.Episode.S01E01.exe",
            "trackedDownloadStatus": "warning",
            "trackedDownloadState": "importPending",
            "statusMessages": [{"title": "x", "messages": ["Found executable file"]}],
        }
    )
    monkeypatch.setattr(check, "_get_json", lambda *a, **k: q)
    ok, msg = check.check_arr_queue()
    assert not ok
    assert "Sonarr" in msg
    assert "Poisoned.Episode.S01E01.exe" in msg


def test_arr_queue_down_on_radarr_warning(monkeypatch):
    monkeypatch.setattr(check, "RADARR_API_KEY", "x")
    q = _queue(
        {
            "title": "Bad.Movie.2026",
            "trackedDownloadStatus": "warning",
            "trackedDownloadState": "importPending",
        }
    )
    monkeypatch.setattr(check, "_get_json", lambda *a, **k: q)
    ok, msg = check.check_arr_queue()
    assert not ok
    assert "Radarr" in msg
    assert "Bad.Movie.2026" in msg


def test_arr_queue_ok_when_both_clean(monkeypatch):
    monkeypatch.setattr(check, "SONARR_API_KEY", "x")
    monkeypatch.setattr(check, "RADARR_API_KEY", "x")
    monkeypatch.setattr(check, "_get_json", lambda *a, **k: _queue())
    ok, msg = check.check_arr_queue()
    assert ok
    assert "Sonarr" in msg and "Radarr" in msg


def test_arr_queue_urls_include_unknown_items_flags(monkeypatch):
    # Both flags default FALSE upstream, hiding exactly the unmapped/poisoned queue items
    # this check exists for. Sonarr got its flag on day one; Radarr's twin was missed
    # (2026-07-02 review M1) — pin BOTH spellings so neither regresses again.
    monkeypatch.setattr(check, "SONARR_API_KEY", "x")
    monkeypatch.setattr(check, "RADARR_API_KEY", "x")
    calls = []

    def fake_get_json(url, headers=None):
        calls.append(url)
        return _queue()

    monkeypatch.setattr(check, "_get_json", fake_get_json)
    ok, _ = check.check_arr_queue()
    assert ok
    sonarr_url = next(u for u in calls if "sonarr" in u)
    radarr_url = next(u for u in calls if "radarr" in u)
    assert "includeUnknownSeriesItems=true" in sonarr_url
    assert "includeUnknownMovieItems=true" in radarr_url


def test_arr_queue_only_checks_configured_app(monkeypatch):
    # Only Sonarr has a key; Radarr must not be queried at all.
    monkeypatch.setattr(check, "SONARR_API_KEY", "x")
    calls = []

    def fake_get_json(url, headers=None):
        calls.append(url)
        return _queue()

    monkeypatch.setattr(check, "_get_json", fake_get_json)
    ok, msg = check.check_arr_queue()
    assert ok
    assert len(calls) == 1
    assert "sonarr" in calls[0]


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


def test_gitops_status_diverged_names_sha():
    ok, msg = check.gitops_status(None, "def456abc7890123")
    assert not ok
    assert "diverged" in msg
    assert "def456ab" in msg


def test_gitops_status_hold_takes_priority_over_diverged():
    ok, msg = check.gitops_status("abc123def4567890", "def456abc7890123")
    assert not ok
    assert "held" in msg


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


def test_check_gitops_status_diverged(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "GITOPS_STATE_DIR", str(tmp_path))
    _gw(tmp_path, "diverged_sha", "def456abc7890123")
    ok, msg = check.check_gitops_status()
    assert not ok
    assert "diverged" in msg


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


# ── wg-easy Pi-peer backup pull (daily host cron writes state.json; we alert on it) ──


def _pi_peers_state(tmp_path, monkeypatch, ts, ok, msg):
    p = tmp_path / "state.json"
    p.write_text(
        '{"ts": %s, "ok": %s, "msg": "%s"}' % (ts, "true" if ok else "false", msg)
    )
    monkeypatch.setattr(check, "PI_PEERS_STATE", str(p))


def test_pi_peers_fresh_success_is_up(tmp_path, monkeypatch):
    _pi_peers_state(
        tmp_path,
        monkeypatch,
        time.time() - 3600,
        True,
        "pulled 3 peer file(s) from daniel-pi",
    )
    ok, msg = check.check_pi_peers()
    assert ok
    assert "3 peer file(s)" in msg


def test_pi_peers_failure_is_down(tmp_path, monkeypatch):
    # A failed pull (Pi unreachable / SSH break) must page — the whole point, since the no-delete
    # pull otherwise leaves stale-but-present keys that keep Backup Freshness green.
    _pi_peers_state(
        tmp_path, monkeypatch, time.time(), False, "rsync exit 255: connection refused"
    )
    ok, msg = check.check_pi_peers()
    assert not ok
    assert "rsync exit 255" in msg


def test_pi_peers_stale_success_is_down(tmp_path, monkeypatch):
    # Daily cadence; a 4d-old success means the pull cron stopped running.
    _pi_peers_state(
        tmp_path, monkeypatch, time.time() - 4 * 86400, True, "pulled 2 peer file(s)"
    )
    ok, msg = check.check_pi_peers()
    assert not ok
    assert "ago" in msg


def test_pi_peers_missing_state_is_down(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "PI_PEERS_STATE", str(tmp_path / "nope.json"))
    ok, msg = check.check_pi_peers()
    assert not ok
    assert "never ran" in msg


def test_pi_peers_unparseable_is_down(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    p.write_text("not json")
    monkeypatch.setattr(check, "PI_PEERS_STATE", str(p))
    ok, msg = check.check_pi_peers()
    assert not ok
    assert "unparseable" in msg


# ── autofix-bridge disk-autoprune host cron (hourly; we alert on it) ──


def test_disk_prune_ok():
    ok, msg = check.disk_prune({"ok": True, "msg": "82% -> 74%"}, 600, 3 * 3600)
    assert ok and "ok" in msg


def test_disk_prune_failed():
    ok, msg = check.disk_prune({"ok": False, "msg": "image prune failed"}, 60, 3 * 3600)
    assert not ok and "FAILED" in msg


def test_disk_prune_stale():
    ok, msg = check.disk_prune({"ok": True, "msg": "x"}, 5 * 3600, 3 * 3600)
    assert not ok and "ago" in msg


# ── CrowdSec home-IP allowlist updater (every-5-min host cron writes state.json; we alert on it) ──


def _home_allowlist_state(tmp_path, monkeypatch, ts, ok, msg):
    p = tmp_path / "state.json"
    p.write_text(
        '{"ts": %s, "ok": %s, "msg": "%s"}' % (ts, "true" if ok else "false", msg)
    )
    monkeypatch.setattr(check, "HOME_ALLOWLIST_STATE", str(p))


def test_home_allowlist_fresh_success_is_up(tmp_path, monkeypatch):
    _home_allowlist_state(
        tmp_path, monkeypatch, time.time() - 120, True, "home IP unchanged (1.2.3.4)"
    )
    ok, msg = check.check_home_allowlist()
    assert ok
    assert "unchanged" in msg


def test_home_allowlist_failure_is_down(tmp_path, monkeypatch):
    # A failed run (ipify unreachable / cscli error) must page — the home path silently starts
    # tripping the WAF on the next IP rotation otherwise.
    _home_allowlist_state(
        tmp_path,
        monkeypatch,
        time.time(),
        False,
        "failed to resolve public IP from ipify",
    )
    ok, msg = check.check_home_allowlist()
    assert not ok
    assert "FAILED" in msg


def test_home_allowlist_stale_success_is_down(tmp_path, monkeypatch):
    # 5-min cadence; a 40-min-old success means the every-5-min cron stopped running.
    _home_allowlist_state(
        tmp_path,
        monkeypatch,
        time.time() - 40 * 60,
        True,
        "home IP unchanged (1.2.3.4)",
    )
    ok, msg = check.check_home_allowlist()
    assert not ok
    assert "min ago" in msg


def test_home_allowlist_missing_state_is_down(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "HOME_ALLOWLIST_STATE", str(tmp_path / "nope.json"))
    ok, msg = check.check_home_allowlist()
    assert not ok
    assert "never ran" in msg


def test_home_allowlist_unparseable_is_down(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    p.write_text("not json")
    monkeypatch.setattr(check, "HOME_ALLOWLIST_STATE", str(p))
    ok, msg = check.check_home_allowlist()
    assert not ok
    assert "unparseable" in msg


# ── Cloudflare-IP drift (weekly host cron diffs cloudflare_ips vs Cloudflare's published ranges) ──


def _cloudflare_drift_state(tmp_path, monkeypatch, ts, ok, msg):
    p = tmp_path / "state.json"
    p.write_text(
        '{"ts": %s, "ok": %s, "msg": "%s"}' % (ts, "true" if ok else "false", msg)
    )
    monkeypatch.setattr(check, "CLOUDFLARE_DRIFT_STATE", str(p))


def test_cloudflare_drift_match_is_up(tmp_path, monkeypatch):
    _cloudflare_drift_state(
        tmp_path, monkeypatch, time.time() - 3600, True, "matches upstream (22 CIDRs)"
    )
    ok, msg = check.check_cloudflare_drift()
    assert ok
    assert "ok" in msg


def test_cloudflare_drift_mismatch_is_down(tmp_path, monkeypatch):
    # A drifted list silently DROPs a client arriving on a newly-added CF range at the origin lock.
    _cloudflare_drift_state(
        tmp_path,
        monkeypatch,
        time.time(),
        False,
        "cloudflare_ips DRIFTED from upstream",
    )
    ok, msg = check.check_cloudflare_drift()
    assert not ok
    assert "drift" in msg.lower()


def test_cloudflare_drift_stale_is_down(tmp_path, monkeypatch):
    # weekly cadence; a 12-day-old success means the weekly cron stopped
    _cloudflare_drift_state(
        tmp_path, monkeypatch, time.time() - 12 * 86400, True, "matches upstream"
    )
    ok, msg = check.check_cloudflare_drift()
    assert not ok
    assert "cron stopped" in msg


def test_cloudflare_drift_missing_state_is_down(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "CLOUDFLARE_DRIFT_STATE", str(tmp_path / "nope.json"))
    ok, msg = check.check_cloudflare_drift()
    assert not ok
    assert "never ran" in msg


def test_appsec_ok_recent_is_up():
    ok, msg = check.appsec(
        {"ok": True, "msg": "3 appsec-configs enabled, 195 inband rules loaded"},
        60,
        2700,
    )
    assert ok
    assert "enforcing" in msg


def test_appsec_failed_assert_is_down():
    # A broken appsec engine (bad cscli collections upgrade / hub rename) leaves the WAF unloaded
    # while crowdsec stays up — the fail-open blind spot this monitor exists to catch.
    ok, msg = check.appsec(
        {"ok": False, "msg": "no enabled appsec-configs — inline WAF not loaded"},
        60,
        2700,
    )
    assert not ok
    assert "not enforcing" in msg


def test_appsec_stale_is_down():
    ok, msg = check.appsec({"ok": True, "msg": "ok"}, 4000, 2700)
    assert not ok
    assert "verify cron stopped" in msg


def test_check_appsec_missing_state_is_down(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "APPSEC_STATE", str(tmp_path / "nope.json"))
    ok, msg = check.check_appsec()
    assert not ok
    assert "never ran" in msg


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


def _dev(wwn, name, collector_date=None, archived=False, device_status=None, temp=None):
    dev = {"wwn": wwn, "device_name": name, "archived": archived}
    if device_status is not None:
        dev["device_status"] = device_status
    smart = {}
    if collector_date:
        smart["collector_date"] = collector_date
    if temp is not None:
        smart["temp"] = temp
    return {"device": dev, "smart": smart or None}


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


# ── scrutiny SMART health (device_status != 0 = a failing drive; freshness alone can't see it) ──


def test_scrutiny_passing_device_is_healthy():
    s = _summary(_dev("w1", "nvme0", device_status=0))
    ok, msg = check.scrutiny_health(s)
    assert ok
    assert "ok" in msg


def test_scrutiny_failed_smart_is_named():
    s = _summary(
        _dev("w1", "nvme0", device_status=1),
        _dev("w2", "sda", device_status=0),
    )
    ok, msg = check.scrutiny_health(s)
    assert not ok
    assert "nvme0" in msg and "SMART self-assessment FAILED" in msg
    assert "sda" not in msg


def test_scrutiny_failed_threshold_is_named():
    s = _summary(_dev("w1", "nvme0", device_status=2))
    ok, msg = check.scrutiny_health(s)
    assert not ok
    assert "attribute threshold breached" in msg


def test_scrutiny_missing_device_status_is_ok():
    # An API that omits device_status must not false-page.
    s = _summary(_dev("w1", "nvme0", "2026-06-06T06:00:00Z"))
    ok, _ = check.scrutiny_health(s)
    assert ok


def test_scrutiny_archived_failing_device_is_skipped():
    s = _summary(_dev("w1", "old-disk", device_status=1, archived=True))
    ok, _ = check.scrutiny_health(s)
    assert ok


def test_scrutiny_temp_ceiling_flags_only_when_enabled():
    s = _summary(_dev("w1", "nvme0", device_status=0, temp=70))
    assert check.scrutiny_health(s, temp_max=0)[0]  # disabled -> ok
    ok, msg = check.scrutiny_health(s, temp_max=60)
    assert not ok
    assert "70" in msg and "60" in msg


# ── ups (battery health via HA's Prometheus-scraped UPS sensors) ─────────────


def test_ups_health_ok():
    ok, msg = check.ups_health(100, 900, 0, 50, 300)
    assert ok
    assert "battery 100%" in msg and "runtime 15.0m" in msg and "self-test ok" in msg


def test_ups_health_low_charge_is_named():
    ok, msg = check.ups_health(30, 900, 0, 50, 300)
    assert not ok
    assert "battery 30%" in msg and "runtime" not in msg


def test_ups_health_low_runtime_is_named():
    ok, msg = check.ups_health(100, 120, 0, 50, 300)
    assert not ok
    assert "runtime 2.0m" in msg and "battery" not in msg


def test_ups_health_both_breaches_named():
    ok, msg = check.ups_health(20, 60, 0, 50, 300)
    assert not ok
    assert "battery 20%" in msg and "runtime 1.0m" in msg


def test_ups_health_replace_battery_pages_even_with_good_runway():
    # The UPS's own RB self-test verdict trips even while charge/runtime read fine — earliest signal.
    ok, msg = check.ups_health(100, 900, 1, 50, 300)
    assert not ok
    assert "replace-battery" in msg


def test_ups_health_at_threshold_is_ok():
    # strict `<`, so exactly at the floor is fine
    assert check.ups_health(50, 300, 0, 50, 300)[0]


def test_ups_health_absent_arm_is_skipped():
    # only runtime present and low -> pages on runtime alone; the other arms are ignored
    ok, msg = check.ups_health(None, 120, None, 50, 300)
    assert not ok
    assert "runtime" in msg and "battery" not in msg


def _ups_scalars(monkeypatch, charge, runtime, replace=0.0):
    def fake(q):
        if q == check.UPS_CHARGE_QUERY:
            return charge
        if q == check.UPS_RUNTIME_QUERY:
            return runtime
        if q == check.UPS_REPLACE_QUERY:
            return replace
        return None

    monkeypatch.setattr(check, "prom_scalar", fake)


def test_check_ups_healthy_is_up(monkeypatch):
    check._ups_down_streak = 0
    _ups_scalars(monkeypatch, 100, 900)
    ok, msg = check.check_ups()
    assert ok and "battery 100%" in msg and "self-test ok" in msg


def test_check_ups_absent_data_defers_to_scrape_targets(monkeypatch):
    check._ups_down_streak = 0
    _ups_scalars(monkeypatch, None, None, replace=None)
    ok, msg = check.check_ups()
    assert ok and "no UPS data" in msg


def test_check_ups_nut_server_down_defers_not_double_pages(monkeypatch):
    # A real NUT-server outage (peanut down / USB unplugged): HA drops the numeric charge+runtime
    # sensors (unavailable) while the replace-battery template FLOORS to 0 (stays present) ->
    # charge=None, runtime=None, replace=0.0. That's the nut container healthcheck's page, NOT an
    # entity rename, so check_ups must DEFER (up) — not partial-absence page with a misdirecting
    # "entity renamed?" msg (the 2026-07-14 review M1 double-page bug).
    check._ups_down_streak = 0
    _ups_scalars(monkeypatch, None, None, replace=0.0)
    ok, msg = check.check_ups()
    assert ok and "NUT numeric arms" in msg
    assert check._ups_down_streak == 0


def test_check_ups_replace_battery_pages(monkeypatch):
    # RB verdict from the self-test -> down after the streak even with a full charge / good runtime.
    check._ups_down_streak = 0
    _ups_scalars(monkeypatch, 100, 900, replace=1.0)
    ok1, _ = check.check_ups()
    assert ok1  # streak grace on the first cycle
    ok2, msg2 = check.check_ups()
    assert not ok2 and "replace-battery" in msg2


def test_check_ups_partial_absence_pages_not_silently_survives(monkeypatch):
    # charge+runtime present but the replace arm vanished (entity rename) -> flag, don't monitor the
    # survivor silently. Goes through the streak (HA-restart grace) then pages, naming the missing arm.
    check._ups_down_streak = 0
    _ups_scalars(monkeypatch, 100, 900, replace=None)
    ok1, msg1 = check.check_ups()
    assert ok1 and "streak 1/2" in msg1
    ok2, msg2 = check.check_ups()
    assert not ok2 and "absent" in msg2 and "replace-battery" in msg2


def test_check_ups_single_low_runtime_is_suppressed_then_pages(monkeypatch):
    check._ups_down_streak = 0
    _ups_scalars(monkeypatch, 100, 60)  # runtime 1m < 5m floor
    ok1, msg1 = check.check_ups()
    assert ok1 and "streak 1/2" in msg1  # UPS_CONSECUTIVE default 2
    ok2, msg2 = check.check_ups()
    assert not ok2 and "runtime" in msg2


def test_check_ups_recovery_resets_streak(monkeypatch):
    check._ups_down_streak = 0
    _ups_scalars(monkeypatch, 100, 60)
    check.check_ups()  # streak advances to 1
    _ups_scalars(monkeypatch, 100, 900)  # healthy again
    ok, _ = check.check_ups()
    assert ok
    assert check._ups_down_streak == 0


def test_check_ups_disabled_when_no_queries(monkeypatch):
    check._ups_down_streak = 0
    monkeypatch.setattr(check, "UPS_CHARGE_QUERY", "")
    monkeypatch.setattr(check, "UPS_RUNTIME_QUERY", "")
    monkeypatch.setattr(check, "UPS_REPLACE_QUERY", "")
    ok, msg = check.check_ups()
    assert ok and "disabled" in msg


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
    # docker_sd-specific failure: the file-tail streams keep flowing, but the highest-volume
    # container-log stream ({container=~".+"}) went silent. The file-tail arm alone stays
    # non-zero and would hide it — the docker-specific arm must page.
    def fake_count(selector, window):
        return 0 if "container" in selector else 500

    monkeypatch.setattr(check, "loki_count", fake_count)
    ok, msg = check.check_loki_ingestion()
    assert not ok
    assert "container" in msg


def test_check_loki_ingestion_filetail_silent_is_down(monkeypatch):
    # file-tail-only failure (the 2026-07-07 blind spot): the docker stream keeps flowing,
    # but authlog/syslog/traefik went silent. Arm 1's selector must EXCLUDE the docker stream
    # (which carries a `container` label) so a healthy container stream can't mask a dead
    # file-tail pipeline — the file-tail arm must page.
    def fake_count(selector, window):
        return 500 if "container" in selector else 0

    monkeypatch.setattr(check, "loki_count", fake_count)
    ok, msg = check.check_loki_ingestion()
    assert not ok
    assert "file-tail" in msg


# --- loki_reachable (the Loki-dependent gate) -------------------------------


def test_loki_reachable_ok(monkeypatch):
    monkeypatch.setattr(
        check, "_get_json", lambda *a, **k: {"status": "success", "data": ["job"]}
    )
    assert check.loki_reachable() is True
    ok, msg = check.check_loki_reachable()
    assert ok
    assert "reachable" in msg.lower()


def test_loki_reachable_non_success_raises(monkeypatch):
    monkeypatch.setattr(check, "_get_json", lambda *a, **k: {"status": "error"})
    with pytest.raises(RuntimeError):
        check.loki_reachable()


# --- janitorr scheduled-task error watchdog --------------------------------
# Gated on Prometheus uptime, NOT content: the boot-race ERROR line ("Unexpected error occurred in
# scheduled task") is generic and identical to a real failure, with the FeignException on a separate
# Loki line — so we skip within the startup grace and count only over the post-startup window.


def test_janitorr_uptime_unknown_is_ok():
    # janitorr stopped/redeployed -> metric absent -> not this check's concern (Restarts/Targets is)
    ok, msg = check.janitorr_errors_ok(5, None, 43200, 600)
    assert ok
    assert "uptime unknown" in msg


def test_janitorr_within_startup_grace_is_ok():
    # up 100s (< 600s grace): the documented post-boot FeignException race window -> never page
    ok, msg = check.janitorr_errors_ok(3, 100, 43200, 600)
    assert ok
    assert "startup grace" in msg


def test_janitorr_error_past_grace_is_down():
    ok, msg = check.janitorr_errors_ok(2, 7200, 43200, 600)
    assert not ok
    assert "2 janitorr scheduled-task error" in msg


def test_janitorr_no_error_past_grace_is_ok():
    ok, msg = check.janitorr_errors_ok(0, 7200, 43200, 600)
    assert ok
    assert "no janitorr errors" in msg


def test_janitorr_none_count_is_ok():
    # No matching Loki series -> None -> 0 -> ok (past grace)
    ok, _ = check.janitorr_errors_ok(None, 7200, 43200, 600)
    assert ok


def test_check_janitorr_uptime_none_skips_loki(monkeypatch):
    # prom_scalar None (janitorr metric absent) -> ok, and loki_count must NOT run
    monkeypatch.setattr(check, "prom_scalar", lambda q: None)

    def boom(*a, **k):
        raise AssertionError("loki_count must not run when uptime is unknown")

    monkeypatch.setattr(check, "loki_count", boom)
    ok, msg = check.check_janitorr()
    assert ok
    assert "uptime unknown" in msg


def test_check_janitorr_within_grace_skips_loki(monkeypatch):
    monkeypatch.setattr(check, "prom_scalar", lambda q: 120.0)  # up 2 min < 600s grace

    def boom(*a, **k):
        raise AssertionError("loki_count must not run within the startup grace")

    monkeypatch.setattr(check, "loki_count", boom)
    ok, msg = check.check_janitorr()
    assert ok
    assert "startup grace" in msg


def test_check_janitorr_past_grace_pages_on_error(monkeypatch):
    monkeypatch.setattr(check, "prom_scalar", lambda q: 100000.0)  # up ~28h
    monkeypatch.setattr(check, "loki_count", lambda selector, window: 1)
    ok, msg = check.check_janitorr()
    assert not ok
    assert "scheduled-task error" in msg


def test_check_janitorr_caps_window_to_post_startup(monkeypatch):
    # up 900s, grace 600s -> effective window is min(12h, 300s) = 300s, so the boot race (all in
    # the first ~minute) can't be counted. Assert the loki_count window arg is the capped value.
    monkeypatch.setattr(check, "prom_scalar", lambda q: 900.0)
    seen = {}

    def fake_count(selector, window):
        seen["window"] = window
        return 0

    monkeypatch.setattr(check, "loki_count", fake_count)
    ok, _ = check.check_janitorr()
    assert ok
    assert seen["window"] == "300s"


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
    monkeypatch.setattr(check, "DISCORD_CROWDSEC_WEBHOOK_URL", "")
    monkeypatch.setattr(check, "DISCORD_GITOPS_WEBHOOK_URL", "")
    ok, msg = check.check_discord()
    assert ok
    assert "disabled" in msg


def test_discord_verifies_all_configured_webhooks(monkeypatch):
    # All three webhooks valid -> up, naming each verified hop.
    monkeypatch.setattr(
        check, "DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/kuma"
    )
    monkeypatch.setattr(
        check,
        "DISCORD_CROWDSEC_WEBHOOK_URL",
        "https://discord.com/api/webhooks/2/crowdsec",
    )
    monkeypatch.setattr(
        check,
        "DISCORD_GITOPS_WEBHOOK_URL",
        "https://discord.com/api/webhooks/3/gitops",
    )
    monkeypatch.setattr(check, "_discord_down_streak", 0)
    monkeypatch.setattr(check, "_get_json", lambda *a, **k: {"name": "Homelab Alerts"})
    ok, msg = check.check_discord()
    assert ok
    assert "Kuma" in msg and "CrowdSec" in msg and "GitOps/Renovate" in msg


def test_discord_gitops_webhook_failure_pages(monkeypatch):
    # A revoked GitOps/Renovate webhook (delivers rollback + Renovate digests, whose "alive"
    # marker greens regardless of delivery — no Kuma backstop) pages, naming it, even though
    # Kuma's own webhook is fine.
    monkeypatch.setattr(
        check, "DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/kuma"
    )
    monkeypatch.setattr(check, "DISCORD_CROWDSEC_WEBHOOK_URL", "")
    monkeypatch.setattr(
        check,
        "DISCORD_GITOPS_WEBHOOK_URL",
        "https://discord.com/api/webhooks/3/gitops",
    )
    monkeypatch.setattr(check, "_discord_down_streak", 0)

    def get(url, *a, **k):
        if "gitops" in url:
            raise urllib.error.HTTPError(url, 404, "gone", {}, None)
        return {"name": "Homelab Alerts"}

    monkeypatch.setattr(check, "_get_json", get)
    assert check.check_discord()[0]  # streak 1, suppressed
    ok, msg = check.check_discord()  # streak 2, pages
    assert not ok
    assert "GitOps/Renovate" in msg and "404" in msg


def test_discord_crowdsec_webhook_failure_pages(monkeypatch):
    # A revoked CrowdSec webhook (the one with no Kuma backstop) pages, naming it — even though
    # Kuma's own webhook is fine.
    monkeypatch.setattr(
        check, "DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/kuma"
    )
    monkeypatch.setattr(
        check,
        "DISCORD_CROWDSEC_WEBHOOK_URL",
        "https://discord.com/api/webhooks/2/crowdsec",
    )
    monkeypatch.setattr(check, "_discord_down_streak", 0)

    def get(url, *a, **k):
        if "crowdsec" in url:
            raise urllib.error.HTTPError(url, 404, "gone", {}, None)
        return {"name": "Homelab Alerts"}

    monkeypatch.setattr(check, "_get_json", get)
    assert check.check_discord()[0]  # streak 1, suppressed
    ok, msg = check.check_discord()  # streak 2, pages
    assert not ok
    assert "CrowdSec" in msg and "404" in msg


def test_discord_healthchecks_webhook_failure_pages(monkeypatch):
    # A revoked healthchecks.io app webhook (its own check-down alerts, no Kuma backstop) pages,
    # naming it — even though Kuma's own webhook is fine.
    monkeypatch.setattr(
        check, "DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/kuma"
    )
    monkeypatch.setattr(
        check,
        "DISCORD_HEALTHCHECKS_WEBHOOK_URL",
        "https://discord.com/api/webhooks/5/hc",
    )
    monkeypatch.setattr(check, "_discord_down_streak", 0)

    def get(url, *a, **k):
        if "/5/hc" in url:
            raise urllib.error.HTTPError(url, 404, "gone", {}, None)
        return {"name": "Homelab Alerts"}

    monkeypatch.setattr(check, "_get_json", get)
    assert check.check_discord()[0]  # streak 1, suppressed
    ok, msg = check.check_discord()  # streak 2, pages
    assert not ok
    assert "Healthchecks" in msg and "404" in msg


# --- email_backstop (throttled SMTP deliverability) -------------------------


def test_email_backstop_disabled_without_password(monkeypatch):
    monkeypatch.setattr(check, "SMTP_PASSWORD", "")
    ok, msg = check.email_backstop()
    assert ok
    assert "disabled" in msg


def test_email_backstop_caches_success_within_interval(monkeypatch):
    monkeypatch.setattr(check, "SMTP_PASSWORD", "app-pw")
    monkeypatch.setattr(check, "EMAIL_PROBE_INTERVAL_S", 3600)
    monkeypatch.setattr(check, "_email_probe", {"ts": 0.0, "ok": True, "msg": ""})
    calls = []

    def probe():
        calls.append(1)
        return True, "SMTP login ok"

    monkeypatch.setattr(check, "_smtp_login_ok", probe)
    assert check.email_backstop(now=10000.0)[0]  # stale ts -> probes
    ok, msg = check.email_backstop(now=11800.0)  # +1800 < interval -> cached
    assert ok and len(calls) == 1 and "verified" in msg
    check.email_backstop(now=13601.0)  # +3601 > interval -> re-probes
    assert len(calls) == 2


def test_email_backstop_failure_reprobes_every_cycle(monkeypatch):
    # a failure is NOT cached (unlike a success), so recovery is caught next cycle, not 6h later
    monkeypatch.setattr(check, "SMTP_PASSWORD", "app-pw")
    monkeypatch.setattr(check, "EMAIL_PROBE_INTERVAL_S", 3600)
    monkeypatch.setattr(check, "_email_probe", {"ts": 0.0, "ok": True, "msg": ""})
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("auth refused")

    monkeypatch.setattr(check, "_smtp_login_ok", boom)
    ok, msg = check.email_backstop(now=10000.0)
    assert not ok and "FAILED" in msg
    ok, _ = check.email_backstop(
        now=10001.0
    )  # 1s later, well within interval -> still re-probes
    assert not ok and len(calls) == 2


def test_check_discord_email_backstop_failure_pages(monkeypatch):
    # webhooks fine but the email 2nd channel's SMTP login fails -> Discord Delivery pages after streak
    monkeypatch.setattr(
        check, "DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/kuma"
    )
    monkeypatch.setattr(check, "DISCORD_CROWDSEC_WEBHOOK_URL", "")
    monkeypatch.setattr(check, "DISCORD_GITOPS_WEBHOOK_URL", "")
    monkeypatch.setattr(check, "DISCORD_ARR_WEBHOOK_URL", "")
    monkeypatch.setattr(check, "DISCORD_HEALTHCHECKS_WEBHOOK_URL", "")
    monkeypatch.setattr(check, "_discord_down_streak", 0)
    monkeypatch.setattr(check, "SMTP_PASSWORD", "app-pw")
    monkeypatch.setattr(check, "_email_probe", {"ts": 0.0, "ok": True, "msg": ""})
    monkeypatch.setattr(check, "_get_json", lambda *a, **k: {"name": "Homelab Alerts"})

    def boom():
        raise RuntimeError("auth refused")

    monkeypatch.setattr(check, "_smtp_login_ok", boom)
    assert check.check_discord()[0]  # streak 1, suppressed
    ok, msg = check.check_discord()  # streak 2, pages
    assert not ok
    assert "email backstop" in msg


# ── Prometheus reachability gate + alert-storm suppression (L1) ──────────────

GB = 10**9


def test_check_prometheus_reachable(monkeypatch):
    monkeypatch.setattr(check, "prom_scalar", lambda q: 1.0)
    ok, msg = check.check_prometheus()
    assert ok
    assert "reachable" in msg.lower()


def test_check_prometheus_no_data_is_down(monkeypatch):
    monkeypatch.setattr(check, "prom_scalar", lambda q: None)
    ok, msg = check.check_prometheus()
    assert not ok


def _wire_run_once(monkeypatch, prom_result):
    """Drive run_once with a tiny CHECKS list (one prom-dependent, one not) and capture pushes.

    Returns (ran, pushes): `ran` is the names of checks actually executed, `pushes` is
    [(token, ok, msg), ...] in push order (incl. the leading `prometheus` push).
    """
    ran, pushes = [], []
    monkeypatch.setattr(
        check, "push", lambda token, ok, msg: pushes.append((token, ok, msg))
    )
    if isinstance(prom_result, Exception):

        def _prom():
            raise prom_result
    else:

        def _prom():
            return prom_result

    monkeypatch.setattr(check, "check_prometheus", _prom)
    # No exporters down by default, so the prom-up path doesn't hit the network probing `up`.
    monkeypatch.setattr(check, "prom_vector", lambda q: [])
    monkeypatch.setattr(check, "PROM_DEPENDENT", frozenset({"disk"}))
    # Loki reachable by default so run_once's Loki gate doesn't make a real network call here.
    monkeypatch.setattr(check, "check_loki_reachable", lambda: (True, "loki ok"))

    def _mk(name):
        def fn():
            ran.append(name)
            return True, "%s ok" % name

        return fn

    monkeypatch.setattr(
        check,
        "CHECKS",
        [("disk", "tok_disk", _mk("disk")), ("backup", "tok_backup", _mk("backup"))],
    )
    check.run_once()
    return ran, pushes


def test_run_once_suppresses_prom_dependent_when_prometheus_down(monkeypatch):
    ran, pushes = _wire_run_once(monkeypatch, (False, "prom is down"))
    # the prom-dependent check is suppressed: never executed, pushed `up` with a skip msg
    assert "disk" not in ran
    assert "backup" in ran  # non-prom check still runs
    by_tok = {tok: (ok, msg) for tok, ok, msg in pushes}
    assert by_tok["tok_disk"][0] is True
    assert "skipped" in by_tok["tok_disk"][1].lower()
    # the Prometheus monitor itself pushed down with its message
    assert any(ok is False and "prom is down" in msg for _, ok, msg in pushes)


def test_run_once_unreachable_prometheus_exception_suppresses(monkeypatch):
    # prom_scalar raising (the real outage path) -> _evaluate renders it down -> suppression
    ran, pushes = _wire_run_once(monkeypatch, RuntimeError("connection refused"))
    assert "disk" not in ran
    assert "backup" in ran
    assert any(ok is False and "connection refused" in msg for _, ok, msg in pushes)


def test_run_once_runs_all_when_prometheus_up(monkeypatch):
    ran, pushes = _wire_run_once(monkeypatch, (True, "ok"))
    assert ran == ["disk", "backup"]  # nothing suppressed
    by_tok = {tok: (ok, msg) for tok, ok, msg in pushes}
    assert "skipped" not in by_tok["tok_disk"][1].lower()


def test_prom_dependent_set_matches_real_checks():
    # Guard: every name in PROM_DEPENDENT is a real check, so the gate can't silently drift.
    names = {name for name, _, _ in check.CHECKS}
    assert check.PROM_DEPENDENT <= names


# ── Loki reachability gate (peer of the Prometheus gate) ─────────────────────


def test_loki_dependent_set_matches_real_checks():
    # Guard (mirrors PROM_DEPENDENT): every name in LOKI_DEPENDENT is a real check.
    names = {name for name, _, _ in check.CHECKS}
    assert check.LOKI_DEPENDENT <= names


def _wire_run_once_loki(monkeypatch, loki_result, checks, loki_dependent):
    """Drive run_once with Prometheus UP and a stubbed Loki-reachability result; capture run+push."""
    ran, pushes = [], []
    monkeypatch.setattr(check, "push", lambda t, ok, m: pushes.append((t, ok, m)))
    monkeypatch.setattr(check, "check_prometheus", lambda: (True, "prom ok"))
    monkeypatch.setattr(check, "prom_vector", lambda q: [])
    monkeypatch.setattr(check, "PROM_DEPENDENT", frozenset())
    monkeypatch.setattr(check, "LOKI_DEPENDENT", frozenset(loki_dependent))
    if isinstance(loki_result, Exception):

        def _loki():
            raise loki_result
    else:

        def _loki():
            return loki_result

    monkeypatch.setattr(check, "check_loki_reachable", _loki)

    def _mk(name):
        def fn():
            ran.append(name)
            return True, "%s ok" % name

        return fn

    monkeypatch.setattr(check, "CHECKS", [(n, "tok_%s" % n, _mk(n)) for n in checks])
    check.run_once()
    return ran, pushes


def test_run_once_suppresses_loki_dependent_when_loki_down(monkeypatch):
    ran, pushes = _wire_run_once_loki(
        monkeypatch,
        (False, "loki unreachable"),
        ["recyclarr", "janitorr", "backup"],
        {"recyclarr", "janitorr"},
    )
    # Loki-dependent checks suppressed (never run, pushed up w/ a skip msg); non-loki still runs
    assert not ({"recyclarr", "janitorr"} & set(ran))
    assert "backup" in ran
    by_tok = {t: (ok, m) for t, ok, m in pushes}
    assert by_tok["tok_recyclarr"][0] is True
    assert "loki" in by_tok["tok_recyclarr"][1].lower()
    # the Loki Reachable monitor itself pushed down with its message
    assert any(ok is False and "loki unreachable" in m for _, ok, m in pushes)


def test_run_once_unreachable_loki_exception_suppresses(monkeypatch):
    # check_loki_reachable raising (the real outage path) -> _evaluate down -> suppression
    ran, _ = _wire_run_once_loki(
        monkeypatch,
        RuntimeError("connection refused"),
        ["recyclarr", "backup"],
        {"recyclarr"},
    )
    assert "recyclarr" not in ran
    assert "backup" in ran


def test_run_once_runs_loki_dependent_when_loki_up(monkeypatch):
    ran, _ = _wire_run_once_loki(
        monkeypatch,
        (True, "Loki reachable"),
        ["recyclarr", "janitorr"],
        {"recyclarr", "janitorr"},
    )
    assert "recyclarr" in ran and "janitorr" in ran


# ── Exporter-reachability gate (node-exporter / cadvisor) — Backups M3 ───────


def test_down_exporters_flags_node_when_node_up_is_zero():
    up = [
        ({"job": "node"}, 0.0),
        ({"job": "cadvisor"}, 1.0),
        ({"job": "prometheus"}, 1.0),
    ]
    assert check.down_exporters(up) == {"node"}


def test_down_exporters_flags_both_when_both_down():
    up = [({"job": "node"}, 0.0), ({"job": "cadvisor"}, 0.0)]
    assert check.down_exporters(up) == {"node", "cadvisor"}


def test_down_exporters_empty_when_all_up():
    up = [({"job": "node"}, 1.0), ({"job": "cadvisor"}, 1.0)]
    assert check.down_exporters(up) == set()


def test_down_exporters_ignores_non_exporter_jobs():
    # A non-exporter target down (e.g. loki) is Scrape Targets' concern, not a suppression trigger.
    up = [({"job": "loki"}, 0.0), ({"job": "node"}, 1.0), ({"job": "cadvisor"}, 1.0)]
    assert check.down_exporters(up) == set()


def test_exporter_dependent_values_are_real_checks():
    # Guard (mirrors PROM_DEPENDENT): every suppressed dependent is a real check name, so the
    # exporter gate can't silently drift, and every dependent is also prom-dependent.
    names = {name for name, _, _ in check.CHECKS}
    for deps in check.EXPORTER_DEPENDENT.values():
        assert deps <= names
        assert deps <= check.PROM_DEPENDENT


def _wire_run_once_prom_up(monkeypatch, up_vector, checks, prom_dependent):
    """Drive run_once with Prometheus UP and a stubbed `up` vector; capture what ran + pushed."""
    ran, pushes = [], []
    monkeypatch.setattr(check, "push", lambda t, ok, m: pushes.append((t, ok, m)))
    monkeypatch.setattr(check, "check_prometheus", lambda: (True, "prom ok"))
    monkeypatch.setattr(check, "prom_vector", lambda q: up_vector if q == "up" else [])
    monkeypatch.setattr(check, "PROM_DEPENDENT", frozenset(prom_dependent))
    monkeypatch.setattr(check, "check_loki_reachable", lambda: (True, "loki ok"))

    def _mk(name):
        def fn():
            ran.append(name)
            return True, "%s ok" % name

        return fn

    monkeypatch.setattr(check, "CHECKS", [(n, "tok_%s" % n, _mk(n)) for n in checks])
    check.run_once()
    return ran, pushes


def test_run_once_suppresses_node_dependents_when_node_exporter_down(monkeypatch):
    up = [({"job": "node"}, 0.0), ({"job": "cadvisor"}, 1.0)]
    ran, pushes = _wire_run_once_prom_up(
        monkeypatch,
        up,
        ["disk", "memory", "b2_trend", "targets"],
        {"disk", "memory", "b2_trend", "targets"},
    )
    # node-dependents suppressed (never run, pushed up with a skip msg); Scrape Targets still pages
    assert not ({"disk", "memory", "b2_trend"} & set(ran))
    assert "targets" in ran
    by_tok = {t: (ok, m) for t, ok, m in pushes}
    assert by_tok["tok_disk"][0] is True
    assert "exporter" in by_tok["tok_disk"][1].lower()


def test_run_once_suppresses_cadvisor_dependents_when_cadvisor_down(monkeypatch):
    up = [({"job": "node"}, 1.0), ({"job": "cadvisor"}, 0.0)]
    ran, _ = _wire_run_once_prom_up(
        monkeypatch,
        up,
        ["restarts", "oom", "cpu", "targets"],
        {"restarts", "oom", "cpu", "targets"},
    )
    assert not ({"restarts", "oom", "cpu"} & set(ran))
    assert "targets" in ran


def test_run_once_no_suppression_when_exporters_up(monkeypatch):
    up = [({"job": "node"}, 1.0), ({"job": "cadvisor"}, 1.0)]
    ran, _ = _wire_run_once_prom_up(
        monkeypatch, up, ["disk", "restarts"], {"disk", "restarts"}
    )
    assert "disk" in ran and "restarts" in ran


def test_run_once_up_probe_failure_does_not_suppress(monkeypatch):
    # If the `up` probe itself errors, fail toward alerting: run the checks, don't mask them.
    def boom(q):
        raise RuntimeError("prom hiccup")

    ran, pushes = [], []
    monkeypatch.setattr(check, "push", lambda t, ok, m: pushes.append((t, ok, m)))
    monkeypatch.setattr(check, "check_prometheus", lambda: (True, "prom ok"))
    monkeypatch.setattr(check, "prom_vector", boom)
    monkeypatch.setattr(check, "PROM_DEPENDENT", frozenset({"disk"}))
    monkeypatch.setattr(check, "check_loki_reachable", lambda: (True, "loki ok"))

    def _mk(name):
        def fn():
            ran.append(name)
            return True, "%s ok" % name

        return fn

    monkeypatch.setattr(check, "CHECKS", [("disk", "tok_disk", _mk("disk"))])
    check.run_once()
    assert "disk" in ran  # not suppressed


# ── B2 usage growth trend (L3) ──────────────────────────────────────────────


def test_b2_trend_flat_is_ok():
    ok, msg = check.b2_trend(5 * GB, 5 * GB, 10 * GB, 7)
    assert ok
    assert "flat" in msg.lower() or "shrink" in msg.lower()


def test_b2_trend_growing_under_cap_is_ok():
    # 5GB now, projected 5.7GB in 7d -> cap is ~50d out, well past the horizon
    ok, msg = check.b2_trend(5 * GB, int(5.7 * GB), 10 * GB, 7)
    assert ok
    assert "horizon" in msg


def test_b2_trend_cap_within_horizon_is_down():
    # 8GB now, projected 11GB in 7d (over the 10GB cap) -> ~5d runway -> down
    ok, msg = check.b2_trend(8 * GB, 11 * GB, 10 * GB, 7)
    assert not ok
    assert "cap" in msg.lower()


def test_b2_trend_missing_metric_is_down():
    ok, msg = check.b2_trend(None, None, 10 * GB, 7)
    assert not ok
    assert "unavailable" in msg.lower()


def test_b2_trend_stale_textfile_is_down():
    # Gauge present + flat (would read ok), but the textfile mtime is older than the max age ->
    # the frozen-gauge blind spot must page instead of reading flat-ok.
    ok, msg = check.b2_trend(
        5 * GB, 5 * GB, 10 * GB, 7, age_s=3 * 86400, max_age_s=2.5 * 86400
    )
    assert not ok
    assert "stale" in msg.lower()


def test_b2_trend_fresh_textfile_passes_through():
    # A fresh mtime must not interfere with the normal flat/ok verdict.
    ok, msg = check.b2_trend(
        5 * GB, 5 * GB, 10 * GB, 7, age_s=3600, max_age_s=2.5 * 86400
    )
    assert ok
    assert "flat" in msg.lower() or "shrink" in msg.lower()


def test_check_b2_trend_uses_predict_linear(monkeypatch):
    queries = []

    def fake_scalar(q):
        queries.append(q)
        if "predict_linear" in q:
            return 11 * GB
        if "mtime" in q:
            return time.time()  # fresh textfile
        return 8 * GB

    monkeypatch.setattr(check, "prom_scalar", fake_scalar)
    ok, msg = check.check_b2_trend()
    assert not ok  # 8GB now, predicted 11GB -> over cap within horizon
    assert any("predict_linear" in q for q in queries)
    assert any("mtime" in q for q in queries)


def test_check_b2_trend_stale_textfile_is_down(monkeypatch):
    def fake_scalar(q):
        if "predict_linear" in q:
            return 5 * GB  # flat
        if "mtime" in q:
            return time.time() - 3 * 86400  # 3d-old textfile, past the 2.5d guard
        return 5 * GB

    monkeypatch.setattr(check, "prom_scalar", fake_scalar)
    ok, msg = check.check_b2_trend()
    assert not ok
    assert "stale" in msg.lower()


# --- indexers_down (pure) ---------------------------------------------------

INX_NOW = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)
INX_NAMES = {1: "EZTV", 2: "1337x", 3: "YTS"}


def _status(*entries):
    """Prowlarr /api/v1/indexerstatus payload from (indexerId, initialFailure) pairs."""
    return [{"indexerId": iid, "initialFailure": init} for iid, init in entries]


def test_indexers_down_flags_indexer_over_threshold():
    status = _status((1, "2026-07-04T11:20:00Z"))  # 40 min ago
    out = check.indexers_down(status, INX_NAMES, INX_NOW, 30)
    assert out == [("EZTV", pytest.approx(40.0, abs=0.1))]


def test_indexers_down_ignores_sub_threshold_flap():
    status = _status((1, "2026-07-04T11:50:00Z"))  # 10 min ago -> below gate
    assert check.indexers_down(status, INX_NAMES, INX_NOW, 30) == []


def test_indexers_down_empty_status_is_clean():
    assert check.indexers_down([], INX_NAMES, INX_NOW, 30) == []


def test_indexers_down_null_initial_failure_skipped():
    assert check.indexers_down(_status((1, None)), INX_NAMES, INX_NOW, 30) == []


def test_indexers_down_malformed_initial_failure_skipped():
    assert (
        check.indexers_down(_status((1, "not-a-timestamp")), INX_NAMES, INX_NOW, 30)
        == []
    )


def test_indexers_down_multiple_sorted_worst_first():
    status = _status(
        (1, "2026-07-04T11:40:00Z"),  # EZTV 20m -> below gate
        (2, "2026-07-04T11:00:00Z"),  # 1337x 60m
        (3, "2026-07-04T11:25:00Z"),  # YTS 35m
    )
    out = check.indexers_down(status, INX_NAMES, INX_NOW, 30)
    assert [n for n, _ in out] == ["1337x", "YTS"]  # 60m before 35m; EZTV excluded


def test_indexers_down_unknown_id_falls_back_to_id_label():
    out = check.indexers_down(
        _status((9, "2026-07-04T11:00:00Z")), INX_NAMES, INX_NOW, 30
    )
    assert out == [("indexer 9", pytest.approx(60.0, abs=0.1))]


def test_indexers_down_skips_ignored_indexer():
    status = _status((1, "2026-07-04T11:00:00Z"))  # EZTV 60m over threshold
    assert check.indexers_down(status, INX_NAMES, INX_NOW, 30, ignore={"eztv"}) == []


def test_indexers_down_ignore_is_case_insensitive():
    status = _status((1, "2026-07-04T11:00:00Z"))  # EZTV 60m, ignore differently cased
    assert check.indexers_down(status, INX_NAMES, INX_NOW, 30, ignore={"EZTV"}) == []


def test_indexers_down_ignore_only_named_indexer():
    status = _status(
        (1, "2026-07-04T11:00:00Z"),  # EZTV 60m -> ignored
        (2, "2026-07-04T11:00:00Z"),  # 1337x 60m -> still flagged
    )
    out = check.indexers_down(status, INX_NAMES, INX_NOW, 30, ignore={"eztv"})
    assert [n for n, _ in out] == ["1337x"]


# --- check_prowlarr_indexers (wrapper) --------------------------------------


def test_prowlarr_indexers_disabled_without_key(monkeypatch):
    monkeypatch.setattr(check, "PROWLARR_API_KEY", "")
    ok, msg = check.check_prowlarr_indexers()
    assert ok is True
    assert "disabled" in msg


def test_prowlarr_indexers_down_on_sustained(monkeypatch):
    monkeypatch.setattr(check, "PROWLARR_API_KEY", "k")
    monkeypatch.setattr(check, "PROWLARR_INDEXER_MIN_DOWN_MIN", 30.0)
    status = _status(
        (1, "2000-01-01T00:00:00Z")
    )  # ancient -> definitely over threshold
    indexers = [{"id": 1, "name": "EZTV"}]
    monkeypatch.setattr(
        check, "_get_json", _seq(status, indexers)
    )  # status, then indexer list
    ok, msg = check.check_prowlarr_indexers()
    assert ok is False
    assert "EZTV down" in msg


def test_prowlarr_indexers_up_when_none_failing(monkeypatch):
    monkeypatch.setattr(check, "PROWLARR_API_KEY", "k")
    monkeypatch.setattr(check, "_get_json", _seq([], [{"id": 1, "name": "EZTV"}]))
    ok, msg = check.check_prowlarr_indexers()
    assert ok is True
    assert "ok" in msg


def test_prowlarr_indexers_ignore_list_suppresses_page(monkeypatch):
    monkeypatch.setattr(check, "PROWLARR_API_KEY", "k")
    monkeypatch.setattr(check, "PROWLARR_INDEXER_IGNORE", "The Pirate Bay")
    status = _status((1, "2000-01-01T00:00:00Z"))  # ancient -> over threshold
    indexers = [{"id": 1, "name": "The Pirate Bay"}]
    monkeypatch.setattr(check, "_get_json", _seq(status, indexers))
    ok, msg = check.check_prowlarr_indexers()
    assert ok is True
    assert "ok" in msg


# --- sanitize (adversary-controlled alert text) — Security L1 ----------------


def test_sanitize_defuses_discord_mentions_and_markdown():
    # A poisoned release title / indexer name must not ping the channel or break formatting.
    out = check.sanitize("@everyone `rm -rf`\nsee @here")
    assert "@" not in out
    assert "`" not in out
    assert "\n" not in out


def test_sanitize_caps_length():
    assert len(check.sanitize("A" * 500)) <= 120


def test_sanitize_handles_none():
    assert check.sanitize(None) == "?"


def test_sanitize_collapses_whitespace():
    assert check.sanitize("a\t b\n\nc") == "a b c"


def test_arr_queue_msg_is_sanitized(monkeypatch):
    # An @everyone-laden release title reaches the alert msg defused, not as a live ping.
    monkeypatch.setattr(check, "SONARR_API_KEY", "k")
    monkeypatch.setattr(check, "RADARR_API_KEY", "")
    queue = {
        "records": [
            {"title": "@everyone Free.Movie", "trackedDownloadStatus": "warning"}
        ]
    }
    monkeypatch.setattr(check, "_get_json", lambda *a, **k: queue)
    ok, msg = check.check_arr_queue()
    assert ok is False
    assert "@everyone" not in msg
    assert "(at)everyone" in msg


# --- CHECKS <-> compose (env + monitors) consistency — CI/CD L2 --------------


def _read_sibling(relpath):
    from pathlib import Path

    return (Path(__file__).resolve().parent / relpath).read_text()


def test_checks_and_compose_push_env_agree():
    # Every KUMA_PUSH_* check.py reads must have a compose `environment:` entry and vice-versa —
    # a check added to CHECKS without its env silently never pushes (empty token) with no Kuma
    # no-heartbeat to self-correct. This is the one wiring axis with no other automated guard.
    import re

    in_code = set(
        re.findall(r'_env\("(KUMA_PUSH_[A-Z0-9_]+)"', _read_sibling("check.py"))
    )
    in_compose = set(
        re.findall(
            r"-\s*(KUMA_PUSH_[A-Z0-9_]+)=",
            _read_sibling("../templates/docker-compose.yml.j2"),
        )
    )
    assert in_code == in_compose, "only in check.py=%s ; only in compose=%s" % (
        sorted(in_code - in_compose),
        sorted(in_compose - in_code),
    )


def test_every_push_token_env_is_wired_to_a_monitor():
    # Each KUMA_PUSH_*={{ var }} env value must also appear as push_token=var in a kuma() label,
    # i.e. an AutoKuma push monitor actually exists to receive what the check pushes.
    import re

    text = _read_sibling("../templates/docker-compose.yml.j2")
    env_vars = set(
        re.findall(r"-\s*KUMA_PUSH_[A-Z0-9_]+=\{\{\s*([a-z0-9_]+)\s*\}\}", text)
    )
    label_vars = set(re.findall(r"push_token=([a-z0-9_]+)", text))
    assert env_vars, "no KUMA_PUSH_* env vars parsed — regex drift?"
    assert env_vars <= label_vars, "env push tokens with no monitor label: %s" % sorted(
        env_vars - label_vars
    )


# --- startup/redeploy grace for the reach-out checks (STARTUP_GRACE) ----------


def test_apply_startup_grace_single_down_is_suppressed():
    # One down cycle (a dependency still starting after the reboot) must NOT page.
    streaks = {}
    ok, msg = check.apply_startup_grace("n8n", False, "Connection refused", 2, streaks)
    assert ok
    assert "1/2" in msg
    assert "startup/redeploy grace" in msg
    assert "Connection refused" in msg  # the real reason is preserved for the log


def test_apply_startup_grace_second_consecutive_down_pages():
    # Default GRACE_CYCLES=2: the 2nd straight down is a genuinely-dead dependency -> down.
    streaks = {}
    assert check.apply_startup_grace("n8n", False, "boom", 2, streaks)[0]
    ok, msg = check.apply_startup_grace("n8n", False, "boom", 2, streaks)
    assert not ok
    assert "boom" in msg
    assert "(2 cycles)" in msg


def test_apply_startup_grace_ok_resets_streak():
    # down, then ok -> never pages, and the streak restarts so the next down is suppressed again.
    streaks = {}
    assert check.apply_startup_grace("backup", False, "down", 2, streaks)[0]
    ok, msg = check.apply_startup_grace("backup", True, "recovered", 2, streaks)
    assert ok
    assert msg == "recovered"
    assert streaks["backup"] == 0
    ok, msg = check.apply_startup_grace("backup", False, "down again", 2, streaks)
    assert ok
    assert "1/2" in msg


def test_apply_startup_grace_streaks_are_per_name():
    # Each monitor keeps its own streak — one flapping check can't age another toward paging.
    streaks = {}
    check.apply_startup_grace("n8n", False, "x", 2, streaks)
    ok, msg = check.apply_startup_grace("arr_queue", False, "y", 2, streaks)
    assert ok
    assert "1/2" in msg  # arr_queue is on its own first cycle, not n8n's second


def test_startup_grace_set_matches_real_checks():
    # Guard (mirrors PROM_DEPENDENT/LOKI_DEPENDENT): every graced name is a real check.
    names = {name for name, _, _ in check.CHECKS}
    assert check.STARTUP_GRACE <= names


def test_startup_grace_disjoint_from_run_once_skip_sets():
    # A graced check must reach the eval path EVERY cycle for its streak to be correct, so it
    # can't also be force-skipped by a reachability gate — STARTUP_GRACE must be disjoint from
    # every run_once skip set (else the streak wouldn't advance while the dependency was down).
    assert check.STARTUP_GRACE.isdisjoint(check.PROM_DEPENDENT)
    assert check.STARTUP_GRACE.isdisjoint(check.LOKI_DEPENDENT)
    for deps in check.EXPORTER_DEPENDENT.values():
        assert check.STARTUP_GRACE.isdisjoint(deps)


def test_startup_grace_covers_every_ungated_reach_out_check():
    # Completeness guard (the 2026-07-14 gap: prowlarr_indexers + scrutiny were reach-out checks
    # structurally identical to the four graced ones, yet omitted). Every check that polls a live
    # app dependency via _get_json — and is NEITHER reachability-gated NOR carrying its own
    # consecutive-streak hysteresis — must be in STARTUP_GRACE, else it false-pages on the
    # weekly-reboot first cycle. A new reach-out check that skips the set trips this test, forcing
    # a conscious classify (add to STARTUP_GRACE, or to the self-hysteresis allowlist below).
    import inspect

    gated = set(check.PROM_DEPENDENT) | set(check.LOKI_DEPENDENT)
    for deps in check.EXPORTER_DEPENDENT.values():
        gated |= set(deps)
    # These ride out the reboot blip with their own down-streak hysteresis instead of the
    # STARTUP_GRACE mechanism (HA_CONSECUTIVE / DISCORD_CONSECUTIVE).
    self_hysteresis = {"ha_heartbeat", "discord"}
    reach_out = {
        name for name, _, fn in check.CHECKS if "_get_json(" in inspect.getsource(fn)
    }
    ungated = reach_out - gated - self_hysteresis
    missing = ungated - check.STARTUP_GRACE
    assert not missing, "ungated reach-out checks missing startup grace: %s" % sorted(
        missing
    )


def _wire_run_once_grace(monkeypatch, results):
    """Drive run_once with Prometheus+Loki UP and one STARTUP_GRACE check whose eval returns
    `results` in order across calls; capture the (ok, msg) pushed for it each cycle."""
    monkeypatch.setattr(check, "check_prometheus", lambda: (True, "prom ok"))
    monkeypatch.setattr(check, "prom_vector", lambda q: [])
    monkeypatch.setattr(check, "check_loki_reachable", lambda: (True, "loki ok"))
    monkeypatch.setattr(check, "PROM_DEPENDENT", frozenset())
    monkeypatch.setattr(check, "LOKI_DEPENDENT", frozenset())
    monkeypatch.setattr(check, "STARTUP_GRACE", frozenset({"n8n"}))
    monkeypatch.setattr(check, "GRACE_CYCLES", 2)
    monkeypatch.setattr(check, "_grace_streaks", {})
    seq = iter(results)
    monkeypatch.setattr(check, "CHECKS", [("n8n", "tok_n8n", lambda: next(seq))])
    pushes = []
    monkeypatch.setattr(check, "push", lambda t, ok, m: pushes.append((t, ok, m)))
    out = []
    for _ in range(len(results)):
        check.run_once()
        out.append(next((ok, m) for t, ok, m in pushes if t == "tok_n8n"))
        pushes.clear()
    return out


def test_run_once_holds_graced_check_up_on_first_down_then_pages(monkeypatch):
    # The weekly-reboot case end to end: first cycle down (dependency mid-start) is held up with a
    # streak msg; a second straight down (dependency really gone) pages with the real reason.
    out = _wire_run_once_grace(
        monkeypatch,
        [(False, "Connection refused"), (False, "Connection refused")],
    )
    assert out[0][0] is True and "1/2" in out[0][1]
    assert out[1][0] is False and "Connection refused" in out[1][1]


def test_run_once_graced_check_recovers_without_paging(monkeypatch):
    # Down then up (the real reboot recovery) never pushes a down for the graced monitor.
    out = _wire_run_once_grace(
        monkeypatch,
        [(False, "Connection refused"), (True, "queue clean")],
    )
    assert out[0][0] is True
    assert out[1] == (True, "queue clean")


# ── DOCKER-USER origin-lock watchdog (traefik verify cron writes state.json; we alert on it) ──


def _docker_user_state(tmp_path, monkeypatch, ts, ok, msg):
    p = tmp_path / "state.json"
    p.write_text(
        '{"ts": %s, "ok": %s, "msg": "%s"}' % (ts, "true" if ok else "false", msg)
    )
    monkeypatch.setattr(check, "DOCKER_USER_STATE", str(p))


def test_docker_user_fresh_success_is_up(tmp_path, monkeypatch):
    _docker_user_state(
        tmp_path, monkeypatch, time.time() - 300, True, "origin lock applied"
    )
    ok, msg = check.check_docker_user()
    assert ok
    assert "verified" in msg


def test_docker_user_failure_is_down(tmp_path, monkeypatch):
    # A flushed chain (terminal DROP gone) must page — the origin is reachable direct otherwise.
    _docker_user_state(
        tmp_path, monkeypatch, time.time(), False, "terminal DROP missing for :443"
    )
    ok, msg = check.check_docker_user()
    assert not ok
    assert "NOT applied" in msg
    assert ":443" in msg


def test_docker_user_stale_success_is_down(tmp_path, monkeypatch):
    # 15-min cadence; a 60-min-old success (past the 45-min window) means the verify cron stopped.
    _docker_user_state(
        tmp_path, monkeypatch, time.time() - 60 * 60, True, "origin lock applied"
    )
    ok, msg = check.check_docker_user()
    assert not ok
    assert "min ago" in msg


def test_docker_user_missing_state_is_down(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "DOCKER_USER_STATE", str(tmp_path / "nope.json"))
    ok, msg = check.check_docker_user()
    assert not ok
    assert "never ran" in msg


def test_docker_user_unparseable_is_down(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    p.write_text("not json")
    monkeypatch.setattr(check, "DOCKER_USER_STATE", str(p))
    ok, msg = check.check_docker_user()
    assert not ok
    assert "unparseable" in msg


# ── quarterly kopia DEEP content verify (quarterly cron writes state.json; we alert on it) ──


def _content_verify_state(tmp_path, monkeypatch, ts, ok, msg):
    p = tmp_path / "state.json"
    p.write_text(
        '{"ts": %s, "ok": %s, "msg": "%s"}' % (ts, "true" if ok else "false", msg)
    )
    monkeypatch.setattr(check, "CONTENT_VERIFY_STATE", str(p))


def test_content_verify_fresh_success_is_up(tmp_path, monkeypatch):
    _content_verify_state(
        tmp_path,
        monkeypatch,
        time.time() - 30 * 86400,
        True,
        "verified 142 snapshots, 0 errors",
    )
    ok, msg = check.check_content_verify()
    assert ok
    assert "142 snapshots" in msg


def test_content_verify_failure_is_down(tmp_path, monkeypatch):
    # A non-zero deep verify (detected bit-rot / unreadable blob on the 25% sample) must page.
    _content_verify_state(
        tmp_path, monkeypatch, time.time(), False, "verify found 2 unreadable objects"
    )
    ok, msg = check.check_content_verify()
    assert not ok
    assert "unreadable" in msg


def test_content_verify_stale_success_is_down(tmp_path, monkeypatch):
    # Quarterly cadence; a 120d-old success (past the 100d window) means the quarterly cron stopped.
    _content_verify_state(tmp_path, monkeypatch, time.time() - 120 * 86400, True, "ok")
    ok, msg = check.check_content_verify()
    assert not ok
    assert "ago" in msg


def test_content_verify_missing_state_is_down(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "CONTENT_VERIFY_STATE", str(tmp_path / "nope.json"))
    ok, msg = check.check_content_verify()
    assert not ok
    assert "never ran" in msg


def test_content_verify_unparseable_is_down(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    p.write_text("not json")
    monkeypatch.setattr(check, "CONTENT_VERIFY_STATE", str(p))
    ok, msg = check.check_content_verify()
    assert not ok
    assert "unparseable" in msg


# ── promtail dropped-entries watchdog (Prometheus counter; partial log loss) ──


def test_promtail_dropped_under_threshold_is_ok():
    ok, msg = check.promtail_dropped(50, "1h", 1000)
    assert ok
    assert "ok" in msg


def test_promtail_dropped_over_threshold_is_down():
    ok, msg = check.promtail_dropped(5000, "1h", 1000)
    assert not ok
    assert "5000" in msg
    assert "partial log loss" in msg


def test_promtail_dropped_none_is_ok():
    # No series (counter never incremented) -> None -> 0 -> up.
    ok, _ = check.promtail_dropped(None, "1h", 1000)
    assert ok


def test_promtail_dropped_at_threshold_is_ok():
    # Exactly at the threshold must NOT alert (strictly greater).
    ok, _ = check.promtail_dropped(1000, "1h", 1000)
    assert ok


def test_check_promtail_dropped_uses_increase(monkeypatch):
    queries = []

    def fake_scalar(q):
        queries.append(q)
        return 5000.0

    monkeypatch.setattr(check, "prom_scalar", fake_scalar)
    ok, _ = check.check_promtail_dropped()
    assert not ok
    # No reason filter — sums drops across ALL reasons (rate_limited/stream_limited/... too, M2).
    assert any(
        "increase(" in q and "promtail_dropped_entries_total" in q and "reason" not in q
        for q in queries
    )
