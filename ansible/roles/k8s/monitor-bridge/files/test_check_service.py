"""Health verdicts for the services: n8n, the *arr queue, gitops, Home Assistant, Loki, R2.

Same shape as the host probes and split from them only by subject — a service check reads an
API and decides, where a host check reads a sensor.
"""

from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

import bridge_config
import bridge_io
import checks_service

_REPO = Path(__file__).resolve().parents[5]

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
    return checks_service.n8n_update_streaks(wf, ex, state, N8N_NOW, window_s)


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
    ok, msg = checks_service.n8n_verdict({"A Flow": 3}, 3, 2, 2)
    assert not ok and "A Flow" in msg and "consecutive" in msg


def test_n8n_verdict_below_consecutive_is_up():
    ok, _ = checks_service.n8n_verdict({"A Flow": 2}, 3, 2, 2)
    assert ok


def test_n8n_verdict_systemic_pages_before_consecutive():
    # two workflows each failing twice (< consecutive_max 3) -> systemic, one alert
    ok, msg = checks_service.n8n_verdict({"A Flow": 2, "B Flow": 2}, 3, 2, 2)
    assert not ok and "systemic" in msg and "2 workflows" in msg


def test_n8n_verdict_two_single_transients_not_systemic():
    # two workflows each failing ONCE (< systemic_streak 2) -> not systemic, not broken -> up
    ok, _ = checks_service.n8n_verdict({"A Flow": 1, "B Flow": 1}, 3, 2, 2)
    assert ok


def test_n8n_verdict_empty_is_up():
    ok, _ = checks_service.n8n_verdict({}, 3, 2, 2)
    assert ok


def test_n8n_missing_stoppedat_falls_back_to_startedat():
    wf = _workflows(("1", "Prod Flow", True))
    ex = {
        "data": [
            {"id": "e1", "workflowId": "1", "status": "error", "startedAt": _n8n_ago(5)}
        ]
    }
    assert checks_service.n8n_update_streaks(wf, ex, {}, N8N_NOW, 7200) == {
        "Prod Flow": 1
    }


def test_n8n_naive_timestamp_treated_as_utc():
    # n8n normally emits UTC 'Z'; a naive timestamp must not raise on the tz-aware compare
    wf = _workflows(("1", "Prod Flow", True))
    naive = (
        (N8N_NOW - timedelta(minutes=5)).replace(tzinfo=None).isoformat()
    )  # no offset/Z
    ex = {
        "data": [{"id": "e1", "workflowId": "1", "status": "error", "stoppedAt": naive}]
    }
    assert checks_service.n8n_update_streaks(wf, ex, {}, N8N_NOW, 7200) == {
        "Prod Flow": 1
    }


def test_n8n_disabled_without_key():
    # N8N_API_KEY defaults to "" in tests -> monitoring disabled, never a false page
    ok, msg = checks_service.check_n8n()
    assert ok
    assert "disabled" in msg.lower()


def test_n8n_check_down_after_consecutive_failures(monkeypatch, seq):
    # a workflow pages only once its streak reaches N8N_CONSECUTIVE_MAX (3) distinct failures
    monkeypatch.setattr(bridge_config, "N8N_API_KEY", "x")
    monkeypatch.setattr(checks_service, "_n8n_streaks", {})
    wf = {"data": [{"id": "1", "name": "Prod Flow", "active": True}]}

    def cycle(eid):
        now_iso = datetime.now(timezone.utc).isoformat()
        ex = {
            "data": [
                {"id": eid, "workflowId": "1", "status": "error", "stoppedAt": now_iso}
            ]
        }
        monkeypatch.setattr(bridge_io, "_get_json", seq(wf, ex))
        return checks_service.check_n8n()

    assert cycle("e1")[0]  # streak 1 -> up
    assert cycle("e2")[0]  # streak 2 -> up
    ok, msg = cycle("e3")  # streak 3 -> down
    assert not ok
    assert "Prod Flow" in msg and "consecutive" in msg


def test_n8n_check_ok_when_no_failures(monkeypatch, seq):
    monkeypatch.setattr(bridge_config, "N8N_API_KEY", "x")
    wf = {"data": [{"id": "1", "name": "Prod Flow", "active": True}]}
    ex = {"data": []}
    monkeypatch.setattr(bridge_io, "_get_json", seq(wf, ex))
    ok, msg = checks_service.check_n8n()
    assert ok
    assert "no active-workflow failures" in msg


def test_n8n_check_single_failure_does_not_page(monkeypatch, seq):
    # one failure -> streak 1 < N8N_CONSECUTIVE_MAX -> stays up (the one-transient grace)
    monkeypatch.setattr(bridge_config, "N8N_API_KEY", "x")
    monkeypatch.setattr(checks_service, "_n8n_streaks", {})
    wf = {"data": [{"id": "1", "name": "Prod Flow", "active": True}]}
    now_iso = datetime.now(timezone.utc).isoformat()
    ex = {
        "data": [
            {"id": "e1", "workflowId": "1", "status": "error", "stoppedAt": now_iso}
        ]
    }
    monkeypatch.setattr(bridge_io, "_get_json", seq(wf, ex))
    ok, _ = checks_service.check_n8n()
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
    offenders = checks_service.queue_warnings(q, "Sonarr")
    assert len(offenders) == 1
    app, title, reason = offenders[0]
    assert app == "Sonarr"
    assert title == "Poisoned.Episode.S01E01.exe"
    assert "executable" in reason


def test_queue_warnings_empty_queue_is_clean():
    assert checks_service.queue_warnings(_queue(), "Radarr") == []


def test_queue_warnings_ignores_normal_downloading_item():
    q = _queue(
        {
            "title": "Some.Movie.2026",
            "trackedDownloadStatus": "ok",
            "trackedDownloadState": "downloading",
        }
    )
    assert checks_service.queue_warnings(q, "Radarr") == []


def test_queue_warnings_flags_import_blocked_state():
    q = _queue(
        {
            "title": "Blocked.Release",
            "trackedDownloadStatus": "ok",
            "trackedDownloadState": "importBlocked",
        }
    )
    offenders = checks_service.queue_warnings(q, "Sonarr")
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
    offenders = checks_service.queue_warnings(q, "Radarr")
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
    offenders = checks_service.queue_warnings(q, "Sonarr")
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
    assert checks_service.queue_warnings(q, "Sonarr") == []


def test_queue_warnings_import_pending_with_messages_is_flagged():
    q = _queue(
        {
            "title": "Ambiguous.Release",
            "trackedDownloadStatus": "ok",
            "trackedDownloadState": "importPending",
            "statusMessages": [{"title": "x", "messages": ["Not a valid video file"]}],
        }
    )
    offenders = checks_service.queue_warnings(q, "Radarr")
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
    offenders = checks_service.queue_warnings(q, "Sonarr")
    titles = {t for _, t, _ in offenders}
    assert titles == {"Bad One", "Bad Two"}


def test_arr_queue_disabled_without_keys():
    # SONARR_API_KEY/RADARR_API_KEY default to "" in tests -> monitoring disabled
    ok, msg = checks_service.check_arr_queue()
    assert ok
    assert "disabled" in msg.lower()


def test_arr_queue_down_on_sonarr_warning(monkeypatch):
    monkeypatch.setattr(bridge_config, "SONARR_API_KEY", "x")
    q = _queue(
        {
            "title": "Poisoned.Episode.S01E01.exe",
            "trackedDownloadStatus": "warning",
            "trackedDownloadState": "importPending",
            "statusMessages": [{"title": "x", "messages": ["Found executable file"]}],
        }
    )
    monkeypatch.setattr(bridge_io, "_get_json", lambda *a, **k: q)
    ok, msg = checks_service.check_arr_queue()
    assert not ok
    assert "Sonarr" in msg
    assert "Poisoned.Episode.S01E01.exe" in msg


def test_arr_queue_down_on_radarr_warning(monkeypatch):
    monkeypatch.setattr(bridge_config, "RADARR_API_KEY", "x")
    q = _queue(
        {
            "title": "Bad.Movie.2026",
            "trackedDownloadStatus": "warning",
            "trackedDownloadState": "importPending",
        }
    )
    monkeypatch.setattr(bridge_io, "_get_json", lambda *a, **k: q)
    ok, msg = checks_service.check_arr_queue()
    assert not ok
    assert "Radarr" in msg
    assert "Bad.Movie.2026" in msg


def test_arr_queue_ok_when_both_clean(monkeypatch):
    monkeypatch.setattr(bridge_config, "SONARR_API_KEY", "x")
    monkeypatch.setattr(bridge_config, "RADARR_API_KEY", "x")
    monkeypatch.setattr(bridge_io, "_get_json", lambda *a, **k: _queue())
    ok, msg = checks_service.check_arr_queue()
    assert ok
    assert "Sonarr" in msg and "Radarr" in msg


def test_arr_queue_urls_include_unknown_items_flags(monkeypatch):
    # Both flags default FALSE upstream, hiding exactly the unmapped/poisoned queue items
    # this check exists for. Sonarr got its flag on day one; Radarr's twin was missed
    # (2026-07-02 review M1) — pin BOTH spellings so neither regresses again.
    monkeypatch.setattr(bridge_config, "SONARR_API_KEY", "x")
    monkeypatch.setattr(bridge_config, "RADARR_API_KEY", "x")
    calls = []

    def fake_get_json(url, headers=None):
        calls.append(url)
        return _queue()

    monkeypatch.setattr(bridge_io, "_get_json", fake_get_json)
    ok, _ = checks_service.check_arr_queue()
    assert ok
    sonarr_url = next(u for u in calls if "sonarr" in u)
    radarr_url = next(u for u in calls if "radarr" in u)
    assert "includeUnknownSeriesItems=true" in sonarr_url
    assert "includeUnknownMovieItems=true" in radarr_url


def test_arr_queue_only_checks_configured_app(monkeypatch):
    # Only Sonarr has a key; Radarr must not be queried at all.
    monkeypatch.setattr(bridge_config, "SONARR_API_KEY", "x")
    calls = []

    def fake_get_json(url, headers=None):
        calls.append(url)
        return _queue()

    monkeypatch.setattr(bridge_io, "_get_json", fake_get_json)
    ok, msg = checks_service.check_arr_queue()
    assert ok
    assert len(calls) == 1
    assert "sonarr" in calls[0]


INX_NOW = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)


INX_NAMES = {1: "EZTV", 2: "1337x", 3: "YTS"}


def _status(*entries):
    """Prowlarr /api/v1/indexerstatus payload from (indexerId, initialFailure) pairs."""
    return [{"indexerId": iid, "initialFailure": init} for iid, init in entries]


def test_indexers_down_flags_indexer_over_threshold():
    status = _status((1, "2026-07-04T11:20:00Z"))  # 40 min ago
    out = checks_service.indexers_down(status, INX_NAMES, INX_NOW, 30)
    assert out == [("EZTV", pytest.approx(40.0, abs=0.1))]


def test_indexers_down_ignores_sub_threshold_flap():
    status = _status((1, "2026-07-04T11:50:00Z"))  # 10 min ago -> below gate
    assert checks_service.indexers_down(status, INX_NAMES, INX_NOW, 30) == []


def test_indexers_down_empty_status_is_clean():
    assert checks_service.indexers_down([], INX_NAMES, INX_NOW, 30) == []


def test_indexers_down_null_initial_failure_skipped():
    assert (
        checks_service.indexers_down(_status((1, None)), INX_NAMES, INX_NOW, 30) == []
    )


def test_indexers_down_malformed_initial_failure_skipped():
    assert (
        checks_service.indexers_down(
            _status((1, "not-a-timestamp")), INX_NAMES, INX_NOW, 30
        )
        == []
    )


def test_indexers_down_multiple_sorted_worst_first():
    status = _status(
        (1, "2026-07-04T11:40:00Z"),  # EZTV 20m -> below gate
        (2, "2026-07-04T11:00:00Z"),  # 1337x 60m
        (3, "2026-07-04T11:25:00Z"),  # YTS 35m
    )
    out = checks_service.indexers_down(status, INX_NAMES, INX_NOW, 30)
    assert [n for n, _ in out] == ["1337x", "YTS"]  # 60m before 35m; EZTV excluded


def test_indexers_down_unknown_id_falls_back_to_id_label():
    out = checks_service.indexers_down(
        _status((9, "2026-07-04T11:00:00Z")), INX_NAMES, INX_NOW, 30
    )
    assert out == [("indexer 9", pytest.approx(60.0, abs=0.1))]


def test_indexers_down_skips_ignored_indexer():
    status = _status((1, "2026-07-04T11:00:00Z"))  # EZTV 60m over threshold
    assert (
        checks_service.indexers_down(status, INX_NAMES, INX_NOW, 30, ignore={"eztv"})
        == []
    )


def test_indexers_down_ignore_is_case_insensitive():
    status = _status((1, "2026-07-04T11:00:00Z"))  # EZTV 60m, ignore differently cased
    assert (
        checks_service.indexers_down(status, INX_NAMES, INX_NOW, 30, ignore={"EZTV"})
        == []
    )


def test_indexers_down_ignore_only_named_indexer():
    status = _status(
        (1, "2026-07-04T11:00:00Z"),  # EZTV 60m -> ignored
        (2, "2026-07-04T11:00:00Z"),  # 1337x 60m -> still flagged
    )
    out = checks_service.indexers_down(status, INX_NAMES, INX_NOW, 30, ignore={"eztv"})
    assert [n for n, _ in out] == ["1337x"]


def test_prowlarr_indexers_disabled_without_key(monkeypatch):
    monkeypatch.setattr(bridge_config, "PROWLARR_API_KEY", "")
    ok, msg = checks_service.check_prowlarr_indexers()
    assert ok is True
    assert "disabled" in msg


def test_prowlarr_indexers_down_on_sustained(monkeypatch, seq):
    monkeypatch.setattr(bridge_config, "PROWLARR_API_KEY", "k")
    monkeypatch.setattr(bridge_config, "PROWLARR_INDEXER_MIN_DOWN_MIN", 30.0)
    status = _status(
        (1, "2000-01-01T00:00:00Z")
    )  # ancient -> definitely over threshold
    indexers = [{"id": 1, "name": "EZTV"}]
    monkeypatch.setattr(
        bridge_io, "_get_json", seq(status, indexers)
    )  # status, then indexer list
    ok, msg = checks_service.check_prowlarr_indexers()
    assert ok is False
    assert "EZTV down" in msg


def test_prowlarr_indexers_up_when_none_failing(monkeypatch, seq):
    monkeypatch.setattr(bridge_config, "PROWLARR_API_KEY", "k")
    monkeypatch.setattr(bridge_io, "_get_json", seq([], [{"id": 1, "name": "EZTV"}]))
    ok, msg = checks_service.check_prowlarr_indexers()
    assert ok is True
    assert "ok" in msg


def test_prowlarr_indexers_ignore_list_suppresses_page(monkeypatch, seq):
    monkeypatch.setattr(bridge_config, "PROWLARR_API_KEY", "k")
    monkeypatch.setattr(bridge_config, "PROWLARR_INDEXER_IGNORE", "The Pirate Bay")
    status = _status((1, "2000-01-01T00:00:00Z"))  # ancient -> over threshold
    indexers = [{"id": 1, "name": "The Pirate Bay"}]
    monkeypatch.setattr(bridge_io, "_get_json", seq(status, indexers))
    ok, msg = checks_service.check_prowlarr_indexers()
    assert ok is True
    assert "ok" in msg


# The case that caught nothing before: a deferred BROAD change never fast-forwards, so the host
# parks on an old tree while last_run keeps ticking and is_diverged stays false. daniel-server ran
# a 12-commit-old tree for hours that way on 2026-08-02 with every GitOps signal green.


# Bazarr holds its OWN copies of Sonarr's and Radarr's API keys, on its PVC and entered
# through its UI, so no deploy updates them. On 2026-08-29 a rotation missed it and the only
# signal was an OOM tile that self-clears after an hour. These pin both directions.
#
# Field values are the ones the live app returned on 2026-08-29 with the keys working.
_HEALTHY_BAZARR = {
    "data": {
        "bazarr_version": "1.5.6",
        "sonarr_version": "4.0.17.2952",
        "radarr_version": "6.1.1.10360",
    }
}


def test_bazarr_healthy_status_and_health_report_nothing():
    """Both peers answering, no self-reported issues."""
    assert checks_service.bazarr_problems(_HEALTHY_BAZARR, {"data": []}) == []


@pytest.mark.parametrize("peer", ["sonarr", "radarr"])
def test_bazarr_empty_peer_version_is_the_stale_key_signal(peer):
    """The 2026-08-29 failure itself.

    Bazarr fills these fields by calling each app with its own stored key, so an
    empty-but-present field is what a rejected key looks like from outside.
    """
    status = {"data": dict(_HEALTHY_BAZARR["data"], **{f"{peer}_version": ""})}

    problems = checks_service.bazarr_problems(status, {"data": []})

    assert len(problems) == 1, problems
    assert peer in problems[0]
    assert "stale API key" in problems[0]


def test_bazarr_absent_peer_version_is_not_a_problem():
    """A disabled integration omits the field entirely.

    Alerting on that would page forever on a peer the operator turned off, so absent and
    empty must not be read alike.
    """
    assert (
        checks_service.bazarr_problems(
            {"data": {"bazarr_version": "1.5.6"}}, {"data": []}
        )
        == []
    )


def test_bazarr_self_reported_health_issues_are_surfaced():
    health = {"data": [{"object": "/tv/Show", "issue": "path does not exist"}]}

    assert checks_service.bazarr_problems(_HEALTHY_BAZARR, health) == [
        "/tv/Show: path does not exist"
    ]


def test_bazarr_check_is_disabled_without_an_api_key(monkeypatch):
    """No key means stay up, the check_n8n convention — not a permanently red monitor."""
    monkeypatch.setattr(bridge_config, "BAZARR_API_KEY", "")

    ok, msg = checks_service.check_bazarr()

    assert ok
    assert "disabled" in msg
