#!/usr/bin/env python3
"""Unit tests for the pure logic in check.py.

Run: uv run pytest ansible/roles/containers/monitor-bridge/files
(or `uv run pytest` for the whole repo suite).

Covers the parts that can be wrong without a live deploy noticing — chiefly the
nanosecond RFC3339 parsing (some sources emit 9 fractional digits; fromisoformat
caps at 6) and each check's decision logic. The HTTP glue is exercised live via
`check.py --once` at deploy time.
"""

import importlib
import os
import re
import time
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

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


# --- n8n consecutive-failure streaks ----------------------------------------

N8N_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)


def _n8n_ago(minutes):
    return (N8N_NOW - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _workflows(*items):
    """items: (id, name, active) tuples -> n8n /workflows payload."""
    return {"data": [{"id": i, "name": n, "active": a} for i, n, a in items]}


def _errors(*items):
    """items: (exec_id, workflowId, ago_min) tuples -> n8n /executions?status=error payload."""
    return {
        "data": [
            {"id": eid, "workflowId": w, "status": "error", "stoppedAt": _n8n_ago(m)}
            for eid, w, m in items
        ]
    }


def _run(wf, ex, state, window_s=7200):
    return check.n8n_update_streaks(wf, ex, state, N8N_NOW, window_s)


def test_n8n_streak_advances_once_per_new_failure():
    wf = _workflows(("1", "Prod Flow", True))
    state = {}
    assert _run(wf, _errors(("e1", "1", 5)), state) == {"Prod Flow": 1}
    # same failure still newest (no new run) -> held at 1, not double-counted across cycles
    assert _run(wf, _errors(("e1", "1", 5)), state) == {"Prod Flow": 1}
    # a new failure -> streak 2
    assert _run(wf, _errors(("e2", "1", 1), ("e1", "1", 5)), state) == {"Prod Flow": 2}


def test_n8n_streak_resets_when_failure_ages_past_window():
    wf = _workflows(("1", "Prod Flow", True))
    state = {"1": {"last_id": "e1", "streak": 2}}
    # newest error is 3h old, window 2h -> recovered/idle -> reset, drops out
    assert _run(wf, _errors(("e1", "1", 180)), state) == {}
    assert state["1"]["streak"] == 0


def test_n8n_streak_resets_when_no_errors_on_record():
    wf = _workflows(("1", "Prod Flow", True))
    state = {"1": {"last_id": "e1", "streak": 2}}
    assert _run(wf, {"data": []}, state) == {}
    assert state["1"]["streak"] == 0


def test_n8n_inactive_workflow_ignored_and_forgotten():
    wf = _workflows(("1", "Draft Flow", False))
    state = {"1": {"last_id": "e1", "streak": 2}}
    assert _run(wf, _errors(("e2", "1", 1)), state) == {}
    assert "1" not in state


def test_n8n_verdict_single_workflow_consecutive_pages():
    ok, msg = check.n8n_verdict({"A Flow": 3}, 3, 2, 2)
    assert not ok and "A Flow" in msg and "consecutive" in msg


def test_n8n_verdict_below_consecutive_is_up():
    ok, _ = check.n8n_verdict({"A Flow": 2}, 3, 2, 2)
    assert ok


def test_n8n_verdict_systemic_pages_before_consecutive():
    # two workflows each failing twice (< consecutive_max 3) -> systemic, one alert
    ok, msg = check.n8n_verdict({"A Flow": 2, "B Flow": 2}, 3, 2, 2)
    assert not ok and "systemic" in msg and "2 workflows" in msg


def test_n8n_verdict_two_single_transients_not_systemic():
    # two workflows each failing ONCE (< systemic_streak 2) -> not systemic, not broken -> up
    ok, _ = check.n8n_verdict({"A Flow": 1, "B Flow": 1}, 3, 2, 2)
    assert ok


def test_n8n_verdict_empty_is_up():
    ok, _ = check.n8n_verdict({}, 3, 2, 2)
    assert ok


def test_n8n_missing_stoppedat_falls_back_to_startedat():
    wf = _workflows(("1", "Prod Flow", True))
    ex = {
        "data": [
            {"id": "e1", "workflowId": "1", "status": "error", "startedAt": _n8n_ago(5)}
        ]
    }
    assert check.n8n_update_streaks(wf, ex, {}, N8N_NOW, 7200) == {"Prod Flow": 1}


def test_n8n_naive_timestamp_treated_as_utc():
    # n8n normally emits UTC 'Z'; a naive timestamp must not raise on the tz-aware compare
    wf = _workflows(("1", "Prod Flow", True))
    naive = (
        (N8N_NOW - timedelta(minutes=5)).replace(tzinfo=None).isoformat()
    )  # no offset/Z
    ex = {
        "data": [{"id": "e1", "workflowId": "1", "status": "error", "stoppedAt": naive}]
    }
    assert check.n8n_update_streaks(wf, ex, {}, N8N_NOW, 7200) == {"Prod Flow": 1}


# --- check_n8n --------------------------------------------------------------


def test_n8n_disabled_without_key():
    # N8N_API_KEY defaults to "" in tests -> monitoring disabled, never a false page
    ok, msg = check.check_n8n()
    assert ok
    assert "disabled" in msg.lower()


def test_n8n_check_down_after_consecutive_failures(monkeypatch):
    # a workflow pages only once its streak reaches N8N_CONSECUTIVE_MAX (3) distinct failures
    monkeypatch.setattr(check, "N8N_API_KEY", "x")
    monkeypatch.setattr(check, "_n8n_streaks", {})
    wf = {"data": [{"id": "1", "name": "Prod Flow", "active": True}]}

    def cycle(eid):
        now_iso = datetime.now(timezone.utc).isoformat()
        ex = {
            "data": [
                {"id": eid, "workflowId": "1", "status": "error", "stoppedAt": now_iso}
            ]
        }
        monkeypatch.setattr(check, "_get_json", _seq(wf, ex))
        return check.check_n8n()

    assert cycle("e1")[0]  # streak 1 -> up
    assert cycle("e2")[0]  # streak 2 -> up
    ok, msg = cycle("e3")  # streak 3 -> down
    assert not ok
    assert "Prod Flow" in msg and "consecutive" in msg


def test_n8n_check_ok_when_no_failures(monkeypatch):
    monkeypatch.setattr(check, "N8N_API_KEY", "x")
    wf = {"data": [{"id": "1", "name": "Prod Flow", "active": True}]}
    ex = {"data": []}
    monkeypatch.setattr(check, "_get_json", _seq(wf, ex))
    ok, msg = check.check_n8n()
    assert ok
    assert "no active-workflow failures" in msg


def test_n8n_check_single_failure_does_not_page(monkeypatch):
    # one failure -> streak 1 < N8N_CONSECUTIVE_MAX -> stays up (the one-transient grace)
    monkeypatch.setattr(check, "N8N_API_KEY", "x")
    monkeypatch.setattr(check, "_n8n_streaks", {})
    wf = {"data": [{"id": "1", "name": "Prod Flow", "active": True}]}
    now_iso = datetime.now(timezone.utc).isoformat()
    ex = {
        "data": [
            {"id": "e1", "workflowId": "1", "status": "error", "stoppedAt": now_iso}
        ]
    }
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


def _ups_scalars(monkeypatch, charge, runtime, replace=0.0, ha_up=None):
    def fake(q):
        if q == check.UPS_CHARGE_QUERY:
            return charge
        if q == check.UPS_RUNTIME_QUERY:
            return runtime
        if q == check.UPS_REPLACE_QUERY:
            return replace
        if q == check.UPS_HA_UP_QUERY:
            return ha_up
        return None

    monkeypatch.setattr(check, "prom_scalar", fake)


def test_check_ups_healthy_is_up(monkeypatch):
    check._ups_down_streak = 0
    _ups_scalars(monkeypatch, 100, 900)
    ok, msg = check.check_ups()
    assert ok and "battery 100%" in msg and "self-test ok" in msg


def test_check_ups_absent_data_defers_to_scrape_targets(monkeypatch):
    # HA scrape down (ha_up None via the fake) -> all arms absent defers to Scrape Targets.
    check._ups_down_streak = 0
    _ups_scalars(monkeypatch, None, None, replace=None)
    ok, msg = check.check_ups()
    assert ok and "no UPS data" in msg


def test_check_ups_all_absent_but_ha_scraping_pages(monkeypatch):
    # Every UPS entity renamed/removed at once while HA keeps scraping (up{home-assistant}==1):
    # Scrape Targets can't see it, so the old all-absent defer silently unmonitored the UPS. Now it
    # pages through the streak (naming the missing arms) instead of deferring.
    check._ups_down_streak = 0
    _ups_scalars(monkeypatch, None, None, replace=None, ha_up=1.0)
    ok1, msg1 = check.check_ups()
    assert ok1 and "streak 1/2" in msg1
    ok2, msg2 = check.check_ups()
    assert not ok2 and "absent" in msg2
    assert check._ups_down_streak == 2


def test_check_ups_all_absent_ha_down_still_defers(monkeypatch):
    # HA scrape affirmatively down (up==0) with all arms absent -> still defer (Scrape Targets owns
    # the HA-source outage); the up-gate only flips the all-absent case to a page when HA is UP.
    check._ups_down_streak = 0
    _ups_scalars(monkeypatch, None, None, replace=None, ha_up=0.0)
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


# ── B2 reachability gate (peer of the Prometheus/Loki gates) ─────────────────


def test_b2_dependent_set_matches_real_checks():
    # Guard (mirrors PROM_DEPENDENT/LOKI_DEPENDENT): every name in B2_DEPENDENT is a real check.
    names = {name for name, _, _ in check.CHECKS}
    assert check.B2_DEPENDENT <= names


def test_b2_dependent_excludes_backup():
    # `backup` polls Kopia live and correctly paged through the 2026-08-02 cap breach — it is the
    # one true signal, so the gate must not suppress it. It is also in STARTUP_GRACE, which has to
    # stay disjoint from every skip set (see test_startup_grace_disjoint_from_run_once_skip_sets).
    assert "backup" not in check.B2_DEPENDENT


def test_cluster_dependent_set_matches_real_checks():
    # Guard (mirrors PROM_DEPENDENT/LOKI_DEPENDENT/B2_DEPENDENT): every name is a real check.
    names = {name for name, _, _ in check.CHECKS}
    assert check.CLUSTER_DEPENDENT <= names


def test_cluster_dependent_disjoint_from_prom_dependent():
    # The whole point of a second gate: k8s_workloads reads the CLUSTER Prometheus, so it must not
    # also be suppressed by the DOCKER Prometheus gate. Being in both would mean a Docker-side
    # outage silences a check whose source is fine, and vice versa.
    assert check.CLUSTER_DEPENDENT.isdisjoint(check.PROM_DEPENDENT)
    assert check.CLUSTER_DEPENDENT.isdisjoint(check.LOKI_DEPENDENT)
    assert check.CLUSTER_DEPENDENT.isdisjoint(check.B2_DEPENDENT)


def test_k8s_workloads_absent_series_is_down_not_up():
    # THE regression this check exists to prevent. `unavailable > 0` returns an empty vector both
    # when everything is healthy and when there are no series at all; reading the healthy meaning
    # onto both is how a monitor goes green while blind.
    ok, msg = check.k8s_workloads_verdict(None, [], 5)
    assert ok is False
    assert "UNKNOWN" in msg


def test_k8s_workloads_partial_series_is_down():
    # A partially-loaded kube-state-metrics — e.g. `apps` dropped from its scoped ClusterRole,
    # which takes every deployment series away while the pod stays up and Ready.
    ok, msg = check.k8s_workloads_verdict(2, [], 5)
    assert ok is False
    assert "below the floor" in msg


def test_k8s_workloads_healthy_when_series_present_and_none_unavailable():
    ok, msg = check.k8s_workloads_verdict(18, [], 5)
    assert ok is True
    assert "18 k8s workloads healthy" == msg


def test_k8s_workloads_names_the_offenders():
    offenders = [
        ({"deployment": "n8n-runners"}, 1.0),
        ({"deployment": "registry"}, 2.0),
    ]
    ok, msg = check.k8s_workloads_verdict(18, offenders, 5)
    assert ok is False
    # Sorted, so the message is stable rather than dependent on Prometheus' series order.
    assert "n8n-runners(1), registry(2)" in msg


def test_k8s_workloads_crash_loop_is_down_despite_available_replicas():
    # The 2026-08-13 homepage incident: a CrashLoopBackOff pod passes readiness for a brief
    # window each backoff cycle, so replica availability read healthy through 31 restarts.
    # The restart counter is the signal that doesn't flap.
    restarts = [({"pod": "homepage-58d867556f-7qbz9"}, 6.0)]
    ok, msg = check.k8s_workloads_verdict(18, [], 5, restarts)
    assert ok is False
    assert "crash-looping" in msg
    assert "homepage-58d867556f-7qbz9(6)" in msg


def test_k8s_workloads_unavailable_replicas_outrank_the_restart_arm():
    # Both arms firing is one incident; the replica message is the more actionable one.
    offenders = [({"deployment": "homepage"}, 1.0)]
    restarts = [({"pod": "homepage-x"}, 6.0)]
    ok, msg = check.k8s_workloads_verdict(18, offenders, 5, restarts)
    assert ok is False
    assert "unavailable replicas" in msg


def test_k8s_daemonsets_absent_series_is_down_not_up():
    # Same fail-closed shape as the deployment arm: an absent DaemonSet series is UNKNOWN,
    # not "no DaemonSets have a problem".
    ok, msg = check.k8s_workloads_verdict(18, [], 5, ds_total=None, min_daemonsets=9)
    assert ok is False
    assert "UNKNOWN" in msg


def test_k8s_daemonsets_partial_series_is_down():
    ok, msg = check.k8s_workloads_verdict(18, [], 5, ds_total=3, min_daemonsets=9)
    assert ok is False
    assert "below the floor" in msg


def test_k8s_daemonsets_names_the_offenders():
    ds_offenders = [({"daemonset": "otel-collector"}, 1.0)]
    ok, msg = check.k8s_workloads_verdict(
        18, [], 5, ds_total=9, ds_offenders=ds_offenders, min_daemonsets=9
    )
    assert ok is False
    assert "otel-collector(1)" in msg


def test_k8s_daemonsets_healthy_alongside_healthy_deployments():
    ok, msg = check.k8s_workloads_verdict(18, [], 5, ds_total=9, min_daemonsets=9)
    assert ok is True
    assert "18 k8s workloads healthy" == msg


# --- estate pinning once one Prometheus holds two estates (slice 3, B5) ------


def test_origin_sel_is_empty_without_a_pin(monkeypatch):
    # Against the Docker Prometheus there is no `origin` label at all — external_labels apply on
    # remote-write and never to local storage — so a pin there would select NOTHING and read as
    # healthy. Empty must stay empty.
    monkeypatch.setattr(check, "PROM_ORIGIN", "")
    assert check.origin_sel() == ""
    assert check.origin_sel('name!=""') == '{name!=""}'


def test_origin_sel_appends_the_pin(monkeypatch):
    monkeypatch.setattr(check, "PROM_ORIGIN", 'origin="daniel-server"')
    assert check.origin_sel() == '{origin="daniel-server"}'
    assert check.origin_sel('name!=""') == '{name!="", origin="daniel-server"}'


def test_origin_pin_derives_from_the_prometheus_url(monkeypatch):
    # THE regression this guards. PROM_ORIGIN is derived rather than configured precisely so it
    # cannot drift out of lockstep with PROMETHEUS_URL: pointing one at the cluster and forgetting
    # the other selects nothing, which every one of these checks decodes as healthy.
    monkeypatch.setenv("PROMETHEUS_URL", "https://prom-k8s.example")
    monkeypatch.setenv("CLUSTER_PROMETHEUS_URL", "https://prom-k8s.example")
    monkeypatch.delenv("PROM_ORIGIN", raising=False)
    reloaded = importlib.reload(check)
    try:
        assert reloaded.PROM_ORIGIN == 'origin="daniel-server"'
    finally:
        monkeypatch.undo()
        importlib.reload(check)


def test_origin_pin_absent_when_reading_the_docker_prometheus(monkeypatch):
    monkeypatch.setenv("PROMETHEUS_URL", "http://prometheus:9090")
    monkeypatch.setenv("CLUSTER_PROMETHEUS_URL", "https://prom-k8s.example")
    monkeypatch.delenv("PROM_ORIGIN", raising=False)
    reloaded = importlib.reload(check)
    try:
        assert reloaded.PROM_ORIGIN == ""
    finally:
        monkeypatch.undo()
        importlib.reload(check)


def test_cluster_targets_is_cluster_dependent_not_prom_dependent():
    # It reads the CLUSTER Prometheus, so a Docker-side outage must not suppress it and vice
    # versa — the same separation k8s_workloads has.
    assert "cluster_targets" in check.CLUSTER_DEPENDENT
    assert "cluster_targets" not in check.PROM_DEPENDENT


def test_cluster_targets_selects_only_cluster_native_series(monkeypatch):
    # origin="" matches series where the label is ABSENT. daniel-server's remote-written series
    # all carry it, so this is exactly the cluster's own set — without the filter this check would
    # re-report the 11 targets its sibling already covers.
    seen = {}

    def fake_vector(promql, base=None, source="prometheus"):
        seen["q"], seen["base"] = promql, base
        return [({"job": "j%d" % i}, 1.0) for i in range(5)]

    monkeypatch.setattr(check, "CLUSTER_PROM_URL", "https://cluster")
    monkeypatch.setattr(check, "prom_vector", fake_vector)
    ok, _ = check.check_cluster_targets()
    assert ok is True
    assert seen["q"] == 'up{origin=""}'
    assert seen["base"] == "https://cluster"


def test_cluster_targets_empty_is_down():
    ok, msg = check.targets_verdict([], check.CLUSTER_TARGETS_MIN)
    assert ok is False
    assert "UNKNOWN" in msg


def test_cluster_targets_disabled_without_cluster_url(monkeypatch):
    monkeypatch.setattr(check, "CLUSTER_PROM_URL", "")
    ok, msg = check.check_cluster_targets()
    assert ok is True
    assert "disabled" in msg


def test_targets_empty_vector_is_down_not_all_clear():
    # THE hole B5 opens. Before the repoint an empty `up` could only mean the queried Prometheus
    # was down, and the PROM_DEPENDENT gate suppressed this check first. Against the cluster copy
    # the gate passes (that Prometheus is fine) while `up{origin="daniel-server"}` is empty, and
    # the old code returned "all 0 targets up".
    ok, msg = check.targets_verdict([], 5)
    assert ok is False
    assert "UNKNOWN" in msg


def test_targets_below_floor_is_down():
    vec = [({"job": "node"}, 1.0), ({"job": "cadvisor"}, 1.0)]
    ok, msg = check.targets_verdict(vec, 5)
    assert ok is False
    assert "below the floor" in msg


def test_targets_names_down_jobs_above_the_floor():
    vec = [({"job": "node"}, 0.0)] + [({"job": "j%d" % i}, 1.0) for i in range(5)]
    ok, msg = check.targets_verdict(vec, 5)
    assert ok is False
    assert "1 target(s) down: node" in msg


def test_targets_all_up_above_the_floor():
    vec = [({"job": "j%d" % i}, 1.0) for i in range(11)]
    ok, msg = check.targets_verdict(vec, 5)
    assert ok is True
    assert msg == "all 11 targets up"


def test_dual_estate_checks_all_pin_the_origin():
    # The five checks whose metrics genuinely exist in BOTH estates (container_start_time_seconds,
    # container_oom_events_total, the container_cpu_cfs_* pair, and `up`). If a new call site is
    # added to one of these without origin_sel, it widens to the whole homelab the moment
    # PROMETHEUS_URL moves — and reports k8s pods as daniel-server offenders.
    source = Path(check.__file__).read_text()
    for metric in (
        "container_start_time_seconds",
        "container_oom_events_total",
        "container_cpu_cfs_throttled_periods_total",
        "container_cpu_cfs_periods_total",
        "container_cpu_cfs_throttled_seconds_total",
    ):
        # A hardcoded `{` straight after the metric name means the matchers bypassed origin_sel.
        assert metric + "{" not in source, (
            "%s uses a literal label block; it must go through origin_sel() so the estate pin "
            "is applied" % metric
        )


def test_duration_seconds_parses_prometheus_durations():
    assert check.duration_seconds("15m") == 900
    assert check.duration_seconds("1h") == 3600
    assert check.duration_seconds("90s") == 90
    assert check.duration_seconds("1d") == 86400
    for bad in ("", "15", "m", "1y", "abc"):
        with pytest.raises(ValueError):
            check.duration_seconds(bad)


def test_k8s_workloads_disabled_without_cluster_url(monkeypatch):
    monkeypatch.setattr(check, "CLUSTER_PROM_URL", "")
    ok, msg = check.check_k8s_workloads()
    assert ok is True
    assert "disabled" in msg


def test_cluster_prometheus_gate_down_when_no_result(monkeypatch):
    monkeypatch.setattr(check, "CLUSTER_PROM_URL", "https://prom-k8s.example")
    monkeypatch.setattr(check, "prom_scalar", lambda *a, **k: None)
    ok, msg = check.check_cluster_prometheus()
    assert ok is False
    assert "no result" in msg


def test_run_once_suppresses_cluster_dependent_when_cluster_prometheus_down(
    monkeypatch,
):
    # A cluster-side outage must page ONCE, as Cluster Prometheus — not as a workload fault.
    pushed = _run_once_with_gates(
        monkeypatch,
        cluster_ok=False,
        checks=[("k8s_workloads", "tok", lambda: (False, "should not run"))],
        cluster_dependent={"k8s_workloads"},
    )
    assert pushed["tok"][0] is True
    assert "cluster Prometheus unreachable" in pushed["tok"][1]


def test_run_once_runs_cluster_dependent_when_cluster_prometheus_up(monkeypatch):
    pushed = _run_once_with_gates(
        monkeypatch,
        cluster_ok=True,
        checks=[("k8s_workloads", "tok", lambda: (False, "real failure"))],
        cluster_dependent={"k8s_workloads"},
    )
    assert pushed["tok"][0] is False
    assert pushed["tok"][1] == "real failure"


def _run_once_with_gates(monkeypatch, cluster_ok, checks, cluster_dependent):
    """Drive run_once with every gate but the cluster one forced healthy."""
    pushed = {}
    monkeypatch.setattr(check, "CHECKS", checks)
    monkeypatch.setattr(check, "STARTUP_GRACE", frozenset())
    monkeypatch.setattr(check, "PROM_DEPENDENT", frozenset())
    monkeypatch.setattr(check, "LOKI_DEPENDENT", frozenset())
    monkeypatch.setattr(check, "B2_DEPENDENT", frozenset())
    monkeypatch.setattr(check, "CLUSTER_DEPENDENT", frozenset(cluster_dependent))
    monkeypatch.setattr(check, "check_prometheus", lambda: (True, "up"))
    monkeypatch.setattr(check, "check_loki_reachable", lambda: (True, "up"))
    monkeypatch.setattr(check, "check_b2_reachable", lambda: (True, "up"))
    monkeypatch.setattr(check, "check_cluster_prometheus", lambda: (cluster_ok, "gate"))
    monkeypatch.setattr(check, "prom_vector", lambda *a, **k: [])
    monkeypatch.setattr(
        check, "push", lambda token, ok, msg: pushed.__setitem__(token, (ok, msg))
    )
    monkeypatch.setattr(check, "log", lambda *a, **k: None)
    check.run_once()
    return pushed


def _reset_b2_probe(monkeypatch, key_id="kid", app_key="akey", interval=1800):
    monkeypatch.setattr(check, "B2_PROBE_KEY_ID", key_id)
    monkeypatch.setattr(check, "B2_PROBE_APPLICATION_KEY", app_key)
    monkeypatch.setattr(check, "B2_PROBE_INTERVAL_S", interval)
    monkeypatch.setattr(
        check, "_b2_probe", {"ts": 0.0, "ok": True, "msg": "not yet probed"}
    )


def test_b2_reachable_disabled_without_credentials(monkeypatch):
    _reset_b2_probe(monkeypatch, key_id="", app_key="")
    ok, msg = check.b2_reachable(now=10_000)
    assert ok is True and "disabled" in msg


def test_b2_authorize_ok_on_account_id(monkeypatch):
    monkeypatch.setattr(check, "B2_PROBE_KEY_ID", "kid")
    monkeypatch.setattr(check, "B2_PROBE_APPLICATION_KEY", "akey")
    monkeypatch.setattr(
        check, "_get_json", lambda url, headers=None: {"accountId": "a1"}
    )
    ok, msg = check.b2_authorize()
    assert ok is True and "reachable" in msg


def test_b2_authorize_accepts_authorization_token_only(monkeypatch):
    # Version-tolerant: Backblaze publishes a v4 body example (accountId top-level) but none for
    # v3, so either field proves it's B2. Pinning one shape would page every cycle if it moved.
    monkeypatch.setattr(check, "B2_PROBE_KEY_ID", "kid")
    monkeypatch.setattr(check, "B2_PROBE_APPLICATION_KEY", "akey")
    monkeypatch.setattr(
        check, "_get_json", lambda url, headers=None: {"authorizationToken": "t"}
    )
    ok, _ = check.b2_authorize()
    assert ok is True


def test_b2_authorize_rejects_unrecognised_response(monkeypatch):
    # A 200 from something that isn't B2 must not read as healthy.
    monkeypatch.setattr(check, "B2_PROBE_KEY_ID", "kid")
    monkeypatch.setattr(check, "B2_PROBE_APPLICATION_KEY", "akey")
    monkeypatch.setattr(check, "_get_json", lambda url, headers=None: {"unexpected": 1})
    ok, msg = check.b2_authorize()
    assert ok is False and "accountId" in msg


def test_b2_reachable_surfaces_the_cap_error_text(monkeypatch):
    # G3: the alert must name the CAUSE. B2 answers a cap breach with transaction_cap_exceeded,
    # and _get_json appends the response body to the HTTPError, so it has to reach the message.
    _reset_b2_probe(monkeypatch)

    def _boom(url, headers=None):
        raise RuntimeError("HTTP Error 403: transaction_cap_exceeded")

    monkeypatch.setattr(check, "_get_json", _boom)
    ok, msg = check.b2_reachable(now=10_000)
    assert ok is False and "transaction_cap_exceeded" in msg


def test_b2_reachable_caches_failure_and_does_not_reprobe(monkeypatch):
    # THE cost-critical property. The fault being detected is a transaction cap, so a failure must
    # NOT re-probe every cycle the way email_backstop does — that would spend the exhausted budget.
    _reset_b2_probe(monkeypatch)
    calls = []

    def _boom(url, headers=None):
        calls.append(url)
        raise RuntimeError("HTTP Error 403: transaction_cap_exceeded")

    monkeypatch.setattr(check, "_get_json", _boom)
    first_ok, _ = check.b2_reachable(now=10_000)
    # five more cycles inside the interval (INTERVAL=300 -> 25 min of cycles)
    for offset in (300, 600, 900, 1200, 1500):
        ok, msg = check.b2_reachable(now=10_000 + offset)
        assert ok is False
        assert (
            "transaction_cap_exceeded" in msg
        )  # cached verdict still reported every cycle
    assert first_ok is False
    assert len(calls) == 1, "a cached failure must not re-probe: %d calls" % len(calls)


def test_b2_reachable_reprobes_after_the_interval(monkeypatch):
    _reset_b2_probe(monkeypatch, interval=1800)
    calls = []

    def _ok(url, headers=None):
        calls.append(url)
        return {"accountId": "a1"}

    monkeypatch.setattr(check, "_get_json", _ok)
    check.b2_reachable(now=10_000)
    check.b2_reachable(now=10_000 + 1799)  # still cached
    assert len(calls) == 1
    check.b2_reachable(now=10_000 + 1801)  # interval elapsed
    assert len(calls) == 2


def _wire_run_once_b2(monkeypatch, b2_result, checks, b2_dependent):
    """Drive run_once with Prometheus+Loki UP and a stubbed B2-reachability result."""
    ran, pushes = [], []
    monkeypatch.setattr(check, "push", lambda t, ok, m: pushes.append((t, ok, m)))
    monkeypatch.setattr(check, "check_prometheus", lambda: (True, "prom ok"))
    monkeypatch.setattr(check, "prom_vector", lambda q: [])
    monkeypatch.setattr(check, "check_loki_reachable", lambda: (True, "loki ok"))
    monkeypatch.setattr(check, "PROM_DEPENDENT", frozenset())
    monkeypatch.setattr(check, "LOKI_DEPENDENT", frozenset())
    monkeypatch.setattr(check, "STARTUP_GRACE", frozenset())
    monkeypatch.setattr(check, "B2_DEPENDENT", frozenset(b2_dependent))
    monkeypatch.setattr(check, "check_b2_reachable", lambda: b2_result)

    def _mk(name):
        def fn():
            ran.append(name)
            return True, "%s ok" % name

        return fn

    monkeypatch.setattr(check, "CHECKS", [(n, "tok_%s" % n, _mk(n)) for n in checks])
    check.run_once()
    return ran, pushes


def test_run_once_suppresses_b2_dependent_when_b2_down(monkeypatch):
    ran, pushes = _wire_run_once_b2(
        monkeypatch,
        (False, "B2 unreachable: HTTP Error 403: transaction_cap_exceeded"),
        ["b2_usage", "verify", "backup"],
        {"b2_usage", "verify"},
    )
    # The four state-file checks stop reporting their last-successful-run as current health...
    assert not ({"b2_usage", "verify"} & set(ran))
    by_tok = {t: (ok, m) for t, ok, m in pushes}
    assert by_tok["tok_b2_usage"][0] is True
    assert "b2" in by_tok["tok_b2_usage"][1].lower()
    # ...while Backup Freshness still runs and can page — it is the signal that was right.
    assert "backup" in ran
    assert any(
        ok is False and "transaction_cap_exceeded" in m for _, ok, m in pushes
    ), "the B2 Reachable monitor must page with B2's own error text"


def test_run_once_runs_b2_dependent_when_b2_up(monkeypatch):
    ran, _ = _wire_run_once_b2(
        monkeypatch,
        (True, "B2 reachable"),
        ["b2_usage", "verify"],
        {"b2_usage", "verify"},
    )
    assert "b2_usage" in ran and "verify" in ran


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
        ["disk", "memory", "targets"],
        {"disk", "memory", "targets"},
    )
    # node-dependents suppressed (never run, pushed up with a skip msg); Scrape Targets still pages
    assert not ({"disk", "memory"} & set(ran))
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
    return (Path(__file__).resolve().parent / relpath).read_text()


def test_checks_and_compose_push_env_agree():
    # Every KUMA_PUSH_* check.py reads must have an env entry in exactly one of the two
    # deployments — the Docker remnant's compose or the cluster twin's env-secret — and
    # vice-versa. A check added to CHECKS without its env silently never pushes (empty
    # token) with no Kuma no-heartbeat to self-correct. Since the Phase F split the two
    # deployments partition the checks (CHECKS_ONLY/CHECKS_SKIP), so the union must equal
    # the code and the halves must not overlap (an overlap = two pushers on one token).

    in_code = set(
        re.findall(r'_env\("(KUMA_PUSH_[A-Z0-9_]+)"', _read_sibling("check.py"))
    )
    in_compose = set(
        re.findall(
            r"-\s*(KUMA_PUSH_[A-Z0-9_]+)=",
            _read_sibling("../templates/docker-compose.yml.j2"),
        )
    )
    in_twin = set(
        re.findall(
            r"^\s*(KUMA_PUSH_[A-Z0-9_]+):",
            _read_sibling("../../../k8s/monitor-bridge/templates/env-secret.yaml.j2"),
            re.MULTILINE,
        )
    )
    assert in_compose.isdisjoint(in_twin), (
        "declared in BOTH deployments (two pushers on one token): %s"
        % sorted(in_compose & in_twin)
    )
    in_deployments = in_compose | in_twin
    assert in_code == in_deployments, "only in check.py=%s ; only in deployments=%s" % (
        sorted(in_code - in_deployments),
        sorted(in_deployments - in_code),
    )


def test_every_push_token_env_is_wired_to_a_monitor():
    # Each KUMA_PUSH_*={{ var }} env value must also appear as push_token=var in a kuma() label,
    # i.e. an AutoKuma push monitor actually exists to receive what the check pushes.

    text = _read_sibling("../templates/docker-compose.yml.j2")
    env_vars = set(
        re.findall(r"-\s*KUMA_PUSH_[A-Z0-9_]+=\{\{\s*([a-z0-9_]+)\s*\}\}", text)
    )
    label_vars = set(re.findall(r"push_token=([a-z0-9_]+)", text))
    assert env_vars, "no KUMA_PUSH_* env vars parsed — regex drift?"
    assert env_vars <= label_vars, "env push tokens with no monitor label: %s" % sorted(
        env_vars - label_vars
    )


# --- down_streak: the shared consecutive-down hysteresis primitive ------------


def test_down_streak_holds_up_below_threshold():
    count, ok, msg = check.down_streak(0, 2, "boom", "grace")
    assert (count, ok) == (1, True)
    assert msg == "down streak 1/2 (grace): boom"


def test_down_streak_pages_at_threshold():
    count, ok, msg = check.down_streak(1, 2, "boom", "grace")
    assert (count, ok) == (2, False)
    assert msg == "boom (2 cycles)"


def test_down_streak_custom_label_and_note():
    _, ok, msg = check.down_streak(
        0, 3, "x", "not alerting yet", held_label="throttling streak"
    )
    assert ok
    assert msg == "throttling streak 1/3 (not alerting yet): x"


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
    assert check.STARTUP_GRACE.isdisjoint(check.B2_DEPENDENT)
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

    gated = (
        set(check.PROM_DEPENDENT) | set(check.LOKI_DEPENDENT) | set(check.B2_DEPENDENT)
    )
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


# --- gitops_status: behind-origin arm ----------------------------------------
# The case that caught nothing before: a deferred BROAD change never fast-forwards, so the host
# parks on an old tree while last_run keeps ticking and is_diverged stays false. daniel-server ran
# a 12-commit-old tree for hours that way on 2026-08-02 with every GitOps signal green.


def test_gitops_status_behind_briefly_is_ok():
    # A routine push leaves the host behind for one tick. That must never page.
    ok, msg = check.gitops_status(None, None, "abc123def4567890 1000.0", now=1600.0)
    assert ok
    assert msg == "no held deploy"


def test_gitops_status_behind_too_long_pages():
    ok, msg = check.gitops_status(
        None, None, "abc123def4567890 1000.0", now=1000.0 + 7 * 3600
    )
    assert not ok
    assert "behind origin" in msg
    assert "abc123de" in msg


def test_gitops_status_behind_respects_threshold_argument():
    ok, _ = check.gitops_status(
        None, None, "abc123def4567890 1000.0", now=1000.0 + 120, max_behind_s=60
    )
    assert not ok


def test_gitops_status_hold_wins_over_behind():
    # A hold leaves the host behind too, but names the actual cause — report that, not the symptom.
    ok, msg = check.gitops_status(
        "held123abc456789", None, "abc123def4567890 1.0", now=1e9
    )
    assert not ok
    assert "held" in msg


def test_gitops_status_diverged_wins_over_behind():
    ok, msg = check.gitops_status(
        None, "div123abc4567890", "abc123def4567890 1.0", now=1e9
    )
    assert not ok
    assert "diverged" in msg


def test_gitops_status_unparseable_behind_marker_is_ok():
    # A garbled marker must read as "not behind" rather than page forever on garbage.
    for marker in ("garbage", "abc123 notanumber", "abc123", ""):
        ok, _ = check.gitops_status(None, None, marker, now=1e9)
        assert ok, marker


# --- fetch-failure messages -------------------------------------------------
#
# The 2026-08-02 B2 transaction-cap outage paged for 13h as "backup check error: timed out",
# which names neither the service nor the cause. These cover what the message must now carry —
# and, just as importantly, what it must never carry.


def test_endpoint_label_keeps_host_and_port():
    assert check.endpoint_label("http://kopia:51515/api/v1/sources") == "kopia:51515"


def test_endpoint_label_omits_the_path():
    """The Discord webhook probe goes through _get_json and its token lives in the PATH.

    Including the path would publish that token into the Kuma message and therefore into
    the very Discord channel it authenticates.
    """
    url = "https://discord.com/api/webhooks/123456789/s3cr3t-token-value"
    label = check.endpoint_label(url)
    assert label == "discord.com"
    assert "s3cr3t" not in label


def test_endpoint_label_omits_query_and_userinfo():
    assert "key" not in check.endpoint_label("http://h:1/p?api_key=key")
    assert check.endpoint_label("http://user:pw@h:1/p") == "h:1"


def test_endpoint_label_survives_a_junk_url():
    assert check.endpoint_label("") == "unknown host"


def test_describe_fetch_failure_names_the_endpoint():
    msg = check.describe_fetch_failure(
        "http://kopia:51515/api/v1/sources", TimeoutError("timed out")
    )
    assert msg == "kopia:51515: timed out"


def test_describe_fetch_failure_surfaces_the_server_body():
    """The body is where the real cause lives — urllib discards it unless read explicitly."""
    msg = check.describe_fetch_failure(
        "http://kopia:51515/api/v1/sources",
        "HTTP 500",
        "AccessDenied: Transaction cap exceeded, see the Caps & Alerts page",
    )
    assert "kopia:51515" in msg
    assert "Transaction cap exceeded" in msg


def test_describe_fetch_failure_collapses_whitespace_and_truncates():
    msg = check.describe_fetch_failure(
        "http://h:1/p", "HTTP 500", "a\n\n  b" + "c" * 500
    )
    assert "a b" in msg  # newlines collapsed — Kuma messages are single-line
    assert len(msg) < 260


def test_describe_fetch_failure_ignores_a_blank_body():
    msg = check.describe_fetch_failure("http://h:1/p", "boom", "   \n ")
    assert msg == "h:1: boom"


def test_get_json_attaches_the_error_body_to_httperror(monkeypatch):
    """The cap string only ever reaches an operator if the body is read off HTTPError.

    urllib exposes it as a one-shot file object that nothing reads by default, so the
    server's own explanation is discarded and the alert says only "HTTP Error 403".
    """
    import io

    body = b'{"error":"AccessDenied: Transaction cap exceeded, see Caps & Alerts"}'

    def boom(*_a, **_k):
        raise urllib.error.HTTPError(
            "http://kopia:51515/api/v1/sources", 403, "Forbidden", {}, io.BytesIO(body)
        )

    monkeypatch.setattr(check.urllib.request, "urlopen", boom)
    with pytest.raises(urllib.error.HTTPError) as ei:
        check._get_json("http://kopia:51515/api/v1/sources")
    # Same type, and .code intact: check_discord branches on it to tell a revoked webhook
    # (decisive 404) from a transient network blip.
    assert ei.value.code == 403
    assert "kopia:51515" in str(ei.value)
    assert "Transaction cap exceeded" in str(ei.value)


def test_get_json_wraps_non_http_errors_without_leaking_the_url(monkeypatch):
    def boom(*_a, **_k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(check.urllib.request, "urlopen", boom)
    url = "https://discord.com/api/webhooks/123/s3cr3t-token"
    with pytest.raises(RuntimeError) as ei:
        check._get_json(url)
    assert "discord.com: timed out" == str(ei.value)
    assert "s3cr3t" not in str(ei.value)


# --- CHECKS_ONLY / CHECKS_SKIP (the Phase F twin/remnant split) ---------------------------

# The remnant's real config: only the five host-state-file checks, every gate off.
REMNANT_ONLY = frozenset(
    {"gitops_alive", "gitops_status", "pi_peers", "disk_prune", "renovate_alive"}
)


def test_check_enabled_only_and_skip_semantics():
    assert check.check_enabled("disk", frozenset(), frozenset())
    assert check.check_enabled("gitops_alive", REMNANT_ONLY, frozenset())
    assert not check.check_enabled("disk", REMNANT_ONLY, frozenset())
    assert not check.check_enabled("disk", frozenset(), frozenset({"disk"}))
    # skip wins even against an explicit only-listing
    assert not check.check_enabled("disk", frozenset({"disk"}), frozenset({"disk"}))


def test_name_set_parses_csv_with_spaces():
    assert check._name_set(" a, b ,c,,") == frozenset({"a", "b", "c"})
    assert check._name_set("") == frozenset()


def test_validate_rejects_unknown_names():
    problems = check.validate_check_filter(
        frozenset({"no_such_check"}), frozenset({"also_bogus"}), check.CHECKS
    )
    assert any("no_such_check" in p for p in problems)
    assert any("also_bogus" in p for p in problems)


def test_validate_rejects_enabled_dependent_with_disabled_gate():
    # Skipping the prometheus gate while its dependents still run would reintroduce the
    # one-outage-N-page storm the gate exists to prevent.
    problems = check.validate_check_filter(
        frozenset(), frozenset({"prometheus"}), check.CHECKS
    )
    assert len(problems) == 1
    assert "gate prometheus is disabled" in problems[0]


def test_validate_accepts_the_remnant_and_twin_configs():
    # The two real deployments: the Docker remnant (state-file checks only, no gates) and
    # the cluster twin (everything except the state-file checks).
    assert check.validate_check_filter(REMNANT_ONLY, frozenset(), check.CHECKS) == []
    assert check.validate_check_filter(frozenset(), REMNANT_ONLY, check.CHECKS) == []


def test_remnant_names_are_real_checks():
    # Guard (mirrors the PROM_DEPENDENT guard): the split set must track CHECKS renames.
    names = {name for name, _, _ in check.CHECKS}
    assert REMNANT_ONLY <= names


def test_run_once_with_remnant_filter_touches_no_gate(monkeypatch):
    # With the remnant's filter active, run_once must evaluate exactly the five state-file
    # checks — no gate probe, no metric check, no push for anything else.
    monkeypatch.setattr(check, "CHECKS_ONLY", REMNANT_ONLY)
    monkeypatch.setattr(check, "CHECKS_SKIP", frozenset())
    evaluated = []
    monkeypatch.setattr(
        check, "_evaluate", lambda name, fn: (evaluated.append(name), (True, "ok"))[1]
    )
    pushed = []
    monkeypatch.setattr(check, "push", lambda token, ok, msg: pushed.append(msg))
    check.run_once()
    assert set(evaluated) == REMNANT_ONLY
    assert len(pushed) == len(REMNANT_ONLY)
