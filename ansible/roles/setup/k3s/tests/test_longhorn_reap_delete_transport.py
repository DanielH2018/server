#!/usr/bin/env python3
"""Unit tests for the two entry points' own delete transport.

Both deletes bypass host_lib.kubectl_runner's fixed 30s subprocess cap -- a Longhorn backup
delete routinely runs past it (module docstring), and kubectl's own --timeout=120s for
snapshots is unreachable behind a 30s client-side kill. Imported directly here (not run as a
subprocess like test_longhorn_reap_entrypoints.py) so `subprocess.run` itself can be
monkeypatched and its kwargs inspected -- a black-box stub kubectl on PATH only ever sees argv,
never the Python-side `timeout=` the caller passed in.

Run: uv run pytest ansible/roles/setup/k3s/tests/test_longhorn_reap_delete_transport.py
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "files"))
import longhorn_reap_orphan_backups as backups_mod
import longhorn_reap_orphan_snapshots as snapshots_mod


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ── backups: no cap ─────────────────────────────────────────────────────────────────────


def test_backups_delete_has_no_subprocess_timeout(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(kwargs)
        return _FakeCompleted()

    monkeypatch.setattr(backups_mod.subprocess, "run", fake_run)
    rc, _out = backups_mod._delete_backup("some-backup")
    assert rc == 0
    assert "timeout" not in calls[0], (
        "a Longhorn backup delete routinely runs past host_lib's 30s cap; capping the "
        "subprocess here would kill an in-progress delete bash's original never capped"
    )


# ── snapshots: cap = the knob plus a margin ─────────────────────────────────────────────


def test_snapshots_delete_timeout_seconds_parses_kubectl_duration_strings():
    assert snapshots_mod._delete_timeout_seconds("120s") == 120
    assert snapshots_mod._delete_timeout_seconds("2m") == 120
    assert snapshots_mod._delete_timeout_seconds("1h") == 3600


def test_snapshots_computed_cap_outlives_the_kubectl_runner_cap_that_made_the_knob_unreachable():
    # Assert the COMPUTED value, not wall time -- a real 120s+margin sleep would make this
    # suite slow for no benefit; this is the number that made --timeout=120s unreachable.
    computed = (
        snapshots_mod._delete_timeout_seconds(snapshots_mod.DELETE_TIMEOUT)
        + snapshots_mod.DELETE_TIMEOUT_MARGIN_S
    )
    assert computed > snapshots_mod.TIMEOUT  # host_lib.kubectl_runner's fixed 30s cap
    assert computed >= snapshots_mod._delete_timeout_seconds(
        snapshots_mod.DELETE_TIMEOUT
    )


def test_snapshots_delete_passes_the_computed_cap_to_the_subprocess(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(kwargs)
        return _FakeCompleted()

    monkeypatch.setattr(snapshots_mod.subprocess, "run", fake_run)
    rc, _out = snapshots_mod._delete_snapshot("some-snap", subprocess_timeout=130)
    assert rc == 0
    assert calls[0]["timeout"] == 130


def test_snapshots_delete_timeout_expired_reports_kubectl_timed_out(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout") or 0.0)

    monkeypatch.setattr(snapshots_mod.subprocess, "run", fake_run)
    rc, out = snapshots_mod._delete_snapshot("some-snap", subprocess_timeout=5)
    assert rc == snapshots_mod.host_lib.KUBECTL_TIMEOUT_RC
    assert "timed out" in out


# ── snapshots: manager-pod readiness ────────────────────────────────────────────────────


def test_purge_treats_a_pod_with_no_containerstatuses_as_ready():
    # jq's `[.status.containerStatuses[].ready] | all` is vacuously TRUE over an empty array;
    # Python's `all()` over an empty iterable agrees -- so a pod reporting none at all still
    # counts as ready, matching bash. `statuses and all(...)` (the earlier form) treated an
    # empty list as NOT ready, which the jq pipeline never did.
    pod = {
        "spec": {"nodeName": "this-node"},
        "status": {"phase": "Running", "podIP": "10.0.0.5"},
    }

    def fake_kubectl(*_args):
        return 0, json.dumps({"items": [pod]})

    # volumes=set() so the per-volume purge loop (a real network POST) never runs -- this test
    # is only about whether a ready backend is FOUND.
    rc = snapshots_mod._purge(fake_kubectl, "this-node", set())
    assert rc == 0


def test_purge_reports_no_ready_pod_when_none_matches():
    def fake_kubectl(*_args):
        return 0, json.dumps({"items": []})

    rc = snapshots_mod._purge(fake_kubectl, "this-node", set())
    assert rc == 1


def test_purge_does_not_treat_a_not_ready_container_as_ready():
    pod = {
        "spec": {"nodeName": "this-node"},
        "status": {
            "phase": "Running",
            "podIP": "10.0.0.5",
            "containerStatuses": [{"ready": False}],
        },
    }

    def fake_kubectl(*_args):
        return 0, json.dumps({"items": [pod]})

    rc = snapshots_mod._purge(fake_kubectl, "this-node", set())
    assert rc == 1
