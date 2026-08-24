"""Health verdicts for the services: n8n, the *arr queue, gitops, Home Assistant, Loki, R2.

Same shape as the host probes and split from them only by subject — a service check reads an
API and decides, where a host check reads a sensor.
"""

import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

import check

_REPO = Path(__file__).resolve().parents[5]


def _seq(*values):
    """Return a callable that yields each value on successive calls (like mock side_effect)."""
    it = iter(values)
    return lambda *a, **k: next(it)


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


@pytest.mark.parametrize(
    ("age_s", "max_age", "ok", "must_contain"),
    [
        pytest.param(60, 5400, True, ("1m ago",), id="fresh"),
        # exactly at max age still counts as alive (<=)
        pytest.param(5400, 5400, True, (), id="at_threshold_is_ok"),
        pytest.param(6000, 5400, False, ("100m ago",), id="stale"),  # 100m > 90m
    ],
)
def test_gitops_alive(age_s, max_age, ok, must_contain):
    result_ok, msg = check.gitops_alive(age_s, max_age)
    assert result_ok is ok
    for s in must_contain:
        assert s in msg


@pytest.mark.parametrize(
    ("hold", "diverged", "ok", "must_contain", "exact_msg"),
    [
        pytest.param(None, None, True, (), "no held deploy", id="no_hold"),
        pytest.param("", None, True, (), None, id="empty_is_ok"),
        pytest.param(
            "abc123def4567890", None, False, ("abc123de",), None, id="held_names_sha"
        ),
        pytest.param(
            None,
            "def456abc7890123",
            False,
            ("diverged", "def456ab"),
            None,
            id="diverged_names_sha",
        ),
        pytest.param(
            "abc123def4567890",
            "def456abc7890123",
            False,
            ("held",),
            None,
            id="hold_takes_priority_over_diverged",
        ),
    ],
)
def test_gitops_status(hold, diverged, ok, must_contain, exact_msg):
    result_ok, msg = check.gitops_status(hold, diverged)
    assert result_ok is ok
    if exact_msg is not None:
        assert msg == exact_msg
    for s in must_contain:
        assert s in msg


def _gw(tmp_path, name, content):
    (tmp_path / name).write_text(content)


@pytest.mark.parametrize(
    ("content_fn", "ok", "must_contain"),
    [
        pytest.param(lambda: str(time.time()), True, (), id="fresh_file"),
        # 100m old > default 90m
        pytest.param(lambda: str(time.time() - 100 * 60), False, (), id="stale_file"),
        pytest.param(None, False, ("no last_run",), id="missing_file"),
        pytest.param(lambda: "not-a-float", False, ("unparseable",), id="unparseable"),
    ],
)
def test_check_gitops_alive(tmp_path, monkeypatch, content_fn, ok, must_contain):
    monkeypatch.setattr(check, "GITOPS_STATE_DIR", str(tmp_path))
    if content_fn is not None:
        _gw(tmp_path, "last_run", content_fn())
    result_ok, msg = check.check_gitops_alive()
    assert result_ok is ok
    for s in must_contain:
        assert s in msg


@pytest.mark.parametrize(
    ("filename", "content", "ok", "must_contain"),
    [
        pytest.param(None, None, True, (), id="no_file_is_ok"),
        pytest.param("hold_sha", "abc123def4567890", False, ("abc123de",), id="held"),
        pytest.param(
            "diverged_sha", "def456abc7890123", False, ("diverged",), id="diverged"
        ),
    ],
)
def test_check_gitops_status(
    tmp_path, monkeypatch, filename, content, ok, must_contain
):
    monkeypatch.setattr(check, "GITOPS_STATE_DIR", str(tmp_path))
    if filename is not None:
        _gw(tmp_path, filename, content)
    result_ok, msg = check.check_gitops_status()
    assert result_ok is ok
    for s in must_contain:
        assert s in msg


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


@pytest.mark.parametrize(
    ("state", "ok", "must_contain"),
    [
        pytest.param(
            _ha_state("2026-06-06T11:59:00Z"), True, ("fresh",), id="fresh_is_ok"
        ),  # 60s old
        pytest.param(
            _ha_state("2026-06-06T11:50:00Z"), False, ("stale",), id="stale_is_down"
        ),  # 600s old
        pytest.param(
            _ha_state("2026-06-06T11:55:00Z"), True, (), id="at_threshold_is_ok"
        ),  # exactly 300s
        pytest.param(
            {"state": "unknown"}, False, (), id="missing_last_changed_is_down"
        ),
        pytest.param(None, False, (), id="none_state_is_down"),
    ],
)
def test_ha_heartbeat_fresh(state, ok, must_contain):
    result_ok, msg = check.ha_heartbeat_fresh(state, 300, now=HB_NOW)
    assert result_ok is ok
    for s in must_contain:
        assert s in msg


# ── check_ha_heartbeat hysteresis (rides out the ~120s deploy/restart) ──────
# A redeploy makes the HTTP API briefly unreachable AND leaves the automation
# scheduler a beat behind, so a single cycle can read unreachable OR stale. Like
# CPU_CONSECUTIVE, only HA_CONSECUTIVE straight down-cycles page; a single blip
# pushes up with a streak msg. ha_heartbeat_fresh uses the real clock (no `now`
# override on this path), so payloads are built relative to real now.
def _ha_payload(age_s):
    lc = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()
    return _ha_state(lc)


def _ha_cycle(monkeypatch, age_s=600, raises=False, banned=0):
    monkeypatch.setattr(check, "HA_URL", "http://home-assistant:8123")
    monkeypatch.setattr(check, "HA_TOKEN", "tok")
    # The ip_ban arm queries Loki via loki_count. Patch it explicitly rather than letting it fall
    # through the _get_json stub below: that stub returns an HA state payload, so the arm would
    # take its fail-open path for an accidental reason and stop testing the hysteresis cleanly.
    monkeypatch.setattr(check, "loki_count", lambda *a, **k: banned)
    if raises:

        def boom(*a, **k):
            raise OSError("connection refused")

        monkeypatch.setattr(check, "_get_json", boom)
    else:
        monkeypatch.setattr(check, "_get_json", lambda *a, **k: _ha_payload(age_s))
    return check.check_ha_heartbeat()


def test_ha_heartbeat_single_stale_cycle_is_suppressed(monkeypatch):
    # One stale cycle (a deploy mid-recreate) must NOT page — pushes up with a streak msg.
    ok, msg = _ha_cycle(monkeypatch, age_s=600)
    assert ok
    assert "1/2" in msg  # streak progress vs default HA_CONSECUTIVE=2


def test_ha_heartbeat_two_consecutive_stale_cycles_alert(monkeypatch):
    # Default HA_CONSECUTIVE=2: the 2nd straight stale cycle is a genuinely wedged HA -> down.
    ok, _ = _ha_cycle(monkeypatch, age_s=600)
    assert ok
    ok, msg = _ha_cycle(monkeypatch, age_s=600)
    assert not ok
    assert "stale" in msg


def test_ha_heartbeat_fresh_read_resets_streak(monkeypatch):
    # stale, then fresh -> never down (a recovered deploy clears the streak).
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
    ok, msg = _ha_cycle(monkeypatch, raises=True)
    assert ok
    assert "1/2" in msg


# ── HA ip_ban arm (2026-08-23: a banned infra IP 403'd the probes into a crash loop) ────────
# HA's ban middleware keys on the peer address, so a burst of bad /api/ calls can ban the node's
# pod-network gateway. The probes now exec curl to 127.0.0.1 and are immune; this arm is what
# keeps the ban itself from being silent.
# Two limits, so this guard is not over-trusted:
#   1. It reads each constant's IN-CODE DEFAULT. `env-secret.yaml.j2` overrides LOKI_STREAM at
#      deploy time, and this role's CLAUDE.md already flags in-code-default != deployed-value as a
#      live trap for exactly that constant. Both happen to select on `job`, so it passes either
#      way — but a deployed override is NOT what is being checked here.
#   2. The vocabulary below came from one k8s pod stream. LOKI_STREAM selects file-tail streams,
#      which may legitimately carry labels this set does not list. Widen the set against a live
#      stream if a genuine selector ever fails — do not delete the guard.
# Promtail's k8s stream vocabulary, read off a live Loki stream on 2026-08-23. `app` is NOT in
# it — HA_BAN_SELECTOR shipped with app="home-assistant", matched no stream, and reported "no
# ip_ban events" forever. A fail-open arm cannot tell "nothing to report" from "wrong question",
# so the selector label has to be checked by something other than the check's own verdict.
# Transcribed from a live stream on 2026-08-23. Kept as a FLOOR rather than the whole answer:
# `filename`, `stream` and `service_name` are added by promtail/Loki itself and appear in no
# config, so deriving alone would under-count and reject a valid selector.
_LOKI_STREAM_LABELS_OBSERVED = frozenset(
    {
        "container",
        "filename",
        "job",
        "machine",
        "namespace",
        "pod",
        "service_name",
        "stream",
    }
)


def _promtail_relabel_targets():
    """`target_label:` values from the rendered promtail config — the labels it actually sets.

    Derived rather than transcribed because the transcription cannot follow a rename: renaming a
    relabel target leaves the frozenset above listing a label nothing emits, so a selector using
    the NEW name is rejected while one using the dead name passes — the guard reporting the
    opposite of the truth. Internal `__foo__` labels are dropped; they never reach a stream.
    """
    cfg = (
        _REPO / "ansible/roles/k8s/loki-homelab/templates/configmap.yaml.j2"
    ).read_text()
    found = set(re.findall(r"target_label:\s*(\S+)", cfg))
    return {label for label in found if not label.startswith("__")}


LOKI_STREAM_LABELS = _LOKI_STREAM_LABELS_OBSERVED | _promtail_relabel_targets()


def test_the_promtail_config_is_actually_readable():
    """A path typo would make _promtail_relabel_targets() return an empty set, silently reducing
    the vocabulary to the transcribed floor and re-opening the gap this closes."""
    assert _promtail_relabel_targets(), (
        "no target_label values parsed from the promtail ConfigMap — the path or the config "
        "shape changed, and the derived half of LOKI_STREAM_LABELS is now inert"
    )


def _selector_labels(selector):
    """Label names in a LogQL stream selector — the `foo` of `{foo="bar",baz=~"qux"}`."""
    head = selector.split("}", 1)[0]
    return set(re.findall(r"(\w+)\s*(?:=~|!~|!=|=)", head))


def test_loki_selectors_use_real_stream_labels():
    for name in ("LOKI_STREAM", "LOKI_DOCKER_STREAM", "HA_BAN_SELECTOR"):
        selector = getattr(check, name)
        unknown = _selector_labels(selector) - LOKI_STREAM_LABELS
        assert not unknown, (
            "%s selects on %s, which promtail does not emit — the query matches no stream and "
            "the check goes permanently green: %s" % (name, sorted(unknown), selector)
        )


def test_ha_ban_no_events_is_ok():
    ok, msg = check.ha_ban_verdict(0, "1h")
    assert ok
    assert "no ip_ban events" in msg


def test_ha_ban_none_series_is_ok():
    # None and 0 are the same healthy answer: HA logs nothing when it bans nobody, so an empty
    # vector is what a healthy cluster looks like — unlike loki_ingestion_fresh, where silence
    # IS the fault.
    ok, _ = check.ha_ban_verdict(None, "1h")
    assert ok


def test_ha_ban_event_is_down():
    ok, msg = check.ha_ban_verdict(1, "1h")
    assert not ok
    assert "ip_ban fired 1 time(s)" in msg
    assert "ip_bans.yaml" in msg


def test_ha_ban_wins_the_message_over_a_healthy_heartbeat(monkeypatch):
    # A ban pages even while the heartbeat itself is fresh — the two arms are independent, and
    # the ban text leads because it names the actionable fault.
    ok, msg = _ha_cycle(monkeypatch, age_s=60, banned=3)
    assert not ok
    assert msg.startswith("HA ip_ban fired 3 time(s)")
    assert "fresh" in msg  # the heartbeat's own verdict is preserved, not dropped


def test_ha_ban_skips_the_deploy_grace(monkeypatch):
    # down_streak exists for transients. A ban persists in /config/ip_bans.yaml until a human
    # clears it, so it must page on the FIRST cycle rather than ride the 2-cycle grace.
    ok, _ = _ha_cycle(monkeypatch, age_s=60, banned=1)
    assert not ok


def test_ha_ban_arm_fails_open_when_loki_errors(monkeypatch):
    # A Loki outage must not page the HA monitor. ha_heartbeat is deliberately NOT in
    # LOKI_DEPENDENT (that would suppress the whole check and blind the real heartbeat), so the
    # arm swallows the error and keeps the heartbeat's verdict.

    def boom(*a, **k):
        raise OSError("loki unreachable")

    monkeypatch.setattr(check, "loki_count", boom)
    monkeypatch.setattr(check, "HA_URL", "http://home-assistant:8123")
    monkeypatch.setattr(check, "HA_TOKEN", "tok")
    monkeypatch.setattr(check, "_get_json", lambda *a, **k: _ha_payload(60))
    ok, msg = check.check_ha_heartbeat()
    assert ok
    assert "ip_ban arm unavailable" in msg


def test_ha_heartbeat_disabled_when_no_url_token(monkeypatch):
    monkeypatch.setattr(check, "HA_URL", "")
    monkeypatch.setattr(check, "HA_TOKEN", "")
    ok, msg = check.check_ha_heartbeat()
    assert ok
    assert "disabled" in msg


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


def _ops(**counts):
    return [
        {"dimensions": {"actionType": a}, "sum": {"requests": n}}
        for a, n in counts.items()
    ]


def test_r2_month_start_is_utc_first_of_month():
    now = datetime(2026, 8, 15, 13, 47, 9, tzinfo=timezone.utc).timestamp()
    assert check.r2_month_start(now) == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_r2_classify_splits_the_two_billed_classes():
    a, b, unknown = check.r2_classify_operations(
        _ops(PutObject=10, UploadPart=5, GetObject=100, HeadObject=7)
    )
    assert (a, b, unknown) == (15, 107, [])


def test_r2_classify_ignores_free_operations():
    a, b, unknown = check.r2_classify_operations(
        _ops(DeleteObject=1000, AbortMultipartUpload=50)
    )
    assert (a, b, unknown) == (0, 0, [])


def test_r2_classify_counts_unknown_actions_as_class_a_and_names_them():
    # Over-counting the tighter arm is the safe direction, and the name explains the jump.
    a, b, unknown = check.r2_classify_operations(_ops(PutObject=1, SomeNewOp=42))
    assert (a, b) == (43, 0)
    assert unknown == ["SomeNewOp"]


def test_r2_verdict_up_well_inside_the_free_tier():
    ok, msg = check.r2_usage_verdict(1_200_000_000, 0, 4_100, 88_000, [])
    assert ok
    assert "storage 1.20/10 GB (12%)" in msg
    assert "Class B 88000/10000000 (1%)" in msg


def test_r2_verdict_down_on_storage_breach():
    ok, msg = check.r2_usage_verdict(8_500_000_000, 0, 0, 0, [])
    assert not ok
    assert "storage at 85%" in msg
    assert "over 80% of free tier" in msg


def test_r2_verdict_down_on_class_b_breach_names_only_that_arm():
    ok, msg = check.r2_usage_verdict(0, 0, 0, 9_000_000, [])
    assert not ok
    assert "Class B at 90%" in msg
    assert "storage at" not in msg


def test_r2_verdict_breaches_at_exactly_the_threshold():
    ok, _ = check.r2_usage_verdict(8_000_000_000, 0, 0, 0, [])
    assert not ok


def test_r2_verdict_down_on_orphaned_multipart_uploads():
    # The quiet storage fill: these bill as bytes and never appear in an object listing.
    ok, msg = check.r2_usage_verdict(0, 500, 0, 0, [])
    assert not ok
    assert "500 incomplete multipart uploads" in msg
    assert "AbortIncompleteMultipartUpload" in msg


def test_r2_verdict_reports_unknown_actions_even_when_up():
    ok, msg = check.r2_usage_verdict(0, 0, 5, 0, ["SomeNewOp"])
    assert ok
    assert "unclassified ops counted as Class A: SomeNewOp" in msg


def test_r2_verdict_treats_a_zero_limit_as_no_limit():
    # A disabled arm must not divide by zero, and must not silently read as 0% used.
    ok, msg = check.r2_usage_verdict(5_000_000_000, 0, 0, 0, [], storage_max_gb=0)
    assert ok
    assert "no limit set" in msg


def _r2_payload(storage=None, operations=None):
    account = {
        "storage": storage if storage is not None else [],
        "operations": operations or [],
    }
    return {"data": {"viewer": {"accounts": [account]}}}


def test_r2_query_parses_storage_and_operations(monkeypatch):
    payload = _r2_payload(
        storage=[{"max": {"payloadSize": 900, "metadataSize": 100, "uploadCount": 3}}],
        operations=_ops(PutObject=2, GetObject=8),
    )
    monkeypatch.setattr(check, "_post_json", lambda *a, **k: payload)
    assert check.r2_query_usage(time.time()) == (1000, 3, 2, 8, [])


def test_r2_query_treats_an_empty_bucket_as_zero_not_a_fault(monkeypatch):
    monkeypatch.setattr(check, "_post_json", lambda *a, **k: _r2_payload())
    assert check.r2_query_usage(time.time()) == (0, 0, 0, 0, [])


def test_r2_query_raises_on_graphql_errors(monkeypatch):
    # A 200 carrying `errors` is how an under-scoped token arrives. Unchecked it would parse as a
    # zero-usage bucket — green while blind.
    monkeypatch.setattr(
        check,
        "_post_json",
        lambda *a, **k: {"data": None, "errors": [{"message": "unauthorized"}]},
    )
    with pytest.raises(RuntimeError, match="unauthorized"):
        check.r2_query_usage(time.time())


def test_r2_query_raises_when_no_account_matches(monkeypatch):
    monkeypatch.setattr(
        check, "_post_json", lambda *a, **k: {"data": {"viewer": {"accounts": []}}}
    )
    with pytest.raises(RuntimeError, match="CF_ACCOUNT_ID"):
        check.r2_query_usage(time.time())


def _arm_r2(monkeypatch):
    monkeypatch.setattr(check, "CF_ACCOUNT_ID", "acct")
    monkeypatch.setattr(check, "CF_ANALYTICS_TOKEN", "tok")
    monkeypatch.setattr(check, "R2_BUCKET", "bucket")
    monkeypatch.setattr(check, "_r2_probe", {"ts": None, "ok": True, "msg": ""})


def test_r2_usage_disabled_without_credentials(monkeypatch):
    monkeypatch.setattr(check, "CF_ANALYTICS_TOKEN", "")
    ok, msg = check.r2_usage(now=1000.0)
    assert ok and "disabled" in msg


def test_r2_usage_caches_a_success(monkeypatch):
    _arm_r2(monkeypatch)
    calls = []
    monkeypatch.setattr(
        check,
        "r2_query_usage",
        lambda now: (calls.append(now), (0, 0, 0, 0, []))[1],
    )
    check.r2_usage(now=1000.0)
    ok, msg = check.r2_usage(now=1000.0 + check.R2_PROBE_INTERVAL_S - 1)
    assert ok
    assert len(calls) == 1
    assert "checked" in msg


def test_r2_usage_reprobes_after_a_failure(monkeypatch):
    # Unlike b2_reachable, a failure is NOT cached: these calls are free, so re-probing costs
    # nothing and finds recovery a cycle sooner.
    _arm_r2(monkeypatch)
    calls = []
    monkeypatch.setattr(
        check,
        "r2_query_usage",
        lambda now: (calls.append(now), (9_000_000_000, 0, 0, 0, []))[1],
    )
    assert not check.r2_usage(now=1000.0)[0]
    assert not check.r2_usage(now=1001.0)[0]
    assert len(calls) == 2
