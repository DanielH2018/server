#!/usr/bin/env python3
"""Unit tests for the two entry points' own delete transport.

Both deletes go through host_lib.kubectl_runner, like the reads, but with a longer timeout
bound to the runner: kubectl's own `--timeout` is a SERVER-side wait, so a client-side cap
under it would kill the process before the templated knob could ever return. Imported directly
here (not run as a subprocess like test_longhorn_reap_entrypoints.py) so `subprocess.run`
itself can be monkeypatched and its kwargs inspected -- a black-box stub kubectl on PATH only
ever sees argv, never the Python-side `timeout=` the caller passed in. The patch target is
`host_lib.subprocess`, the module the runner's closure actually reads.

Run: uv run pytest ansible/roles/setup/k3s/tests/test_longhorn_reap_delete_transport.py
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "files"))
import host_lib
import longhorn_reap_orphan_backups as backups_mod
import longhorn_reap_orphan_snapshots as snapshots_mod


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeResponse:
    """Stands in for what `urlopen` yields; `_purge` uses it only as a context manager."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _record_subprocess_run(monkeypatch, calls, result=None):
    """Capture every `subprocess.run` the shared kubectl runner makes, returning `result`."""

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return result or _FakeCompleted()

    monkeypatch.setattr(host_lib.subprocess, "run", fake_run)


# ── backups: bounded on both sides ──────────────────────────────────────────────────────


def test_backups_delete_passes_both_the_server_wait_and_the_subprocess_cap(monkeypatch):
    calls = []
    _record_subprocess_run(monkeypatch, calls)
    rc, _out = backups_mod._delete_backup("some-backup")
    assert rc == 0
    argv, kwargs = calls[0]
    assert "--timeout=%ds" % backups_mod.DELETE_TIMEOUT_S in argv
    assert kwargs["timeout"] == (
        backups_mod.DELETE_TIMEOUT_S + backups_mod.DELETE_TIMEOUT_MARGIN_S
    ), (
        "an unbounded backup delete hangs the whole run with nothing reported -- the sibling "
        "snapshot reaper wedged for 23 minutes that way on 2026-08-16"
    )


def test_backups_computed_cap_outlives_the_server_side_wait():
    # Assert the COMPUTED value, not wall time: the client cap must never fire first and turn a
    # delete that is still legitimately running on the server into a false FAILED.
    assert (
        backups_mod.DELETE_TIMEOUT_S + backups_mod.DELETE_TIMEOUT_MARGIN_S
        > backups_mod.DELETE_TIMEOUT_S
    )
    assert backups_mod.DELETE_TIMEOUT_S + backups_mod.DELETE_TIMEOUT_MARGIN_S > (
        backups_mod.TIMEOUT
    )  # host_lib.kubectl_runner's read cap, which a backup delete routinely runs past


def test_backups_delete_timeout_expired_reports_kubectl_timed_out(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout") or 0.0)

    monkeypatch.setattr(host_lib.subprocess, "run", fake_run)
    rc, out = backups_mod._delete_backup("some-backup")
    assert rc == host_lib.KUBECTL_TIMEOUT_RC
    assert "timed out" in out


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
    # TIMEOUT is parsed lazily inside main() (see longhorn_reap_orphan_snapshots.py's
    # _TIMEOUT_RAW), so the module itself carries only the raw env string.
    assert computed > int(
        snapshots_mod._TIMEOUT_RAW
    )  # host_lib.kubectl_runner's 30s default
    assert computed >= snapshots_mod._delete_timeout_seconds(
        snapshots_mod.DELETE_TIMEOUT
    )


def test_snapshots_delete_passes_the_computed_cap_to_the_subprocess(monkeypatch):
    calls = []
    _record_subprocess_run(monkeypatch, calls)
    rc, _out = snapshots_mod._delete_snapshot("some-snap", subprocess_timeout=130)
    assert rc == 0
    argv, kwargs = calls[0]
    assert "--timeout=%s" % snapshots_mod.DELETE_TIMEOUT in argv
    assert kwargs["timeout"] == 130


def test_snapshots_delete_timeout_expired_reports_kubectl_timed_out(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout") or 0.0)

    monkeypatch.setattr(host_lib.subprocess, "run", fake_run)
    rc, out = snapshots_mod._delete_snapshot("some-snap", subprocess_timeout=5)
    assert rc == host_lib.KUBECTL_TIMEOUT_RC
    assert "timed out" in out


# ── snapshots: manager-pod readiness ────────────────────────────────────────────────────


def _pod(container_statuses=None):
    status = {"phase": "Running", "podIP": "10.0.0.5"}
    if container_statuses is not None:
        status["containerStatuses"] = container_statuses
    return {"spec": {"nodeName": "this-node"}, "status": status}


def _kubectl_returning(*pods):
    def fake_kubectl(*_args):
        return 0, json.dumps({"items": list(pods)})

    return fake_kubectl


def test_purge_accepts_a_pod_whose_containers_are_all_ready():
    # volumes=set() so the per-volume POST loop never runs -- this pair is only about whether a
    # ready backend is FOUND.
    assert (
        snapshots_mod._purge(
            _kubectl_returning(_pod([{"ready": True}])), "this-node", set()
        )
        == 0
    )


def test_purge_rejects_a_pod_with_no_containerstatuses():
    # The rejecting half. An earlier comment here claimed bash's
    # `select([.status.containerStatuses[].ready] | all)` counted such a pod as vacuously ready;
    # it did not -- `.[]` over a null field raises "Cannot iterate over null", jq exits nonzero,
    # and bash took its "no ready manager pod" branch. A pod whose containers have not started
    # cannot serve the purge POST.
    assert snapshots_mod._purge(_kubectl_returning(_pod()), "this-node", {"vol-a"}) == 1


def test_purge_rejects_a_not_ready_container():
    assert (
        snapshots_mod._purge(
            _kubectl_returning(_pod([{"ready": False}])), "this-node", {"vol-a"}
        )
        == 1
    )


def test_purge_reports_no_ready_pod_when_none_matches():
    assert snapshots_mod._purge(_kubectl_returning(), "this-node", {"vol-a"}) == 1


# ── snapshots: a rejected purge POST is counted ─────────────────────────────────────────


def test_purge_returns_zero_when_every_post_is_accepted(monkeypatch):
    monkeypatch.setattr(
        snapshots_mod.urllib.request, "urlopen", lambda *a, **k: _FakeResponse()
    )
    rc = snapshots_mod._purge(
        _kubectl_returning(_pod([{"ready": True}])), "this-node", {"vol-a", "vol-b"}
    )
    assert rc == 0


def test_purge_counts_every_rejected_post(monkeypatch):
    def reject(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(snapshots_mod.urllib.request, "urlopen", reject)
    rc = snapshots_mod._purge(
        _kubectl_returning(_pod([{"ready": True}])), "this-node", {"vol-a", "vol-b"}
    )
    assert rc == 2, (
        "a run whose purges were all rejected reclaims nothing; returning 0 made it exit as a "
        "success with every deleted snapshot marked removed but never coalesced"
    )
