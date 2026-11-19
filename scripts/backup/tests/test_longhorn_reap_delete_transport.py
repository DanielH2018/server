#!/usr/bin/env python3
"""Unit tests for the two entry points' own delete transport.

Both deletes go through host_lib.kubectl_runner, like the reads, but with a longer timeout
bound to the runner: kubectl's own `--timeout` is a SERVER-side wait, so a client-side cap
under it would kill the process before the server-side bound could ever return. Imported directly
here (not run as a subprocess like test_longhorn_reap_entrypoints.py) so `subprocess.run`
itself can be monkeypatched and its kwargs inspected -- a black-box stub kubectl on PATH only
ever sees argv, never the Python-side `timeout=` the caller passed in. The patch target is
`host_lib.subprocess`, the module the runner's closure actually reads.

Run: uv run pytest scripts/backup/tests/test_longhorn_reap_delete_transport.py
"""

import json
import subprocess

import host_lib
from longhorn_reap_lib import backups as backups_mod
from longhorn_reap_lib import logic
from longhorn_reap_lib import snapshots as snapshots_mod


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


DEFAULTS = logic.parse_reap_config({})


def test_backups_delete_passes_both_the_server_wait_and_the_subprocess_cap(monkeypatch):
    calls = []
    _record_subprocess_run(monkeypatch, calls)
    rc, _out = backups_mod._delete_backup("some-backup", DEFAULTS)
    assert rc == 0
    argv, kwargs = calls[0]
    assert "--timeout=120s" in argv
    assert kwargs["timeout"] == 120 + logic.DELETE_TIMEOUT_MARGIN_S, (
        "an unbounded backup delete hangs the whole run with nothing reported -- the sibling "
        "snapshot reaper wedged for 23 minutes that way on 2026-08-16"
    )


def test_the_default_delete_cap_outlives_the_server_wait_and_the_read_cap():
    # Assert the COMPUTED value, not wall time: the client cap must never fire first and turn a
    # delete that is still legitimately running on the server into a false FAILED.
    assert DEFAULTS.delete_subprocess_timeout_s > DEFAULTS.delete_timeout_s
    # host_lib.kubectl_runner's read cap, which a delete routinely runs past.
    assert DEFAULTS.delete_subprocess_timeout_s > DEFAULTS.kubectl_timeout_s


def test_backups_delete_timeout_expired_reports_kubectl_timed_out(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout") or 0.0)

    monkeypatch.setattr(host_lib.subprocess, "run", fake_run)
    rc, out = backups_mod._delete_backup("some-backup", DEFAULTS)
    assert rc == host_lib.KUBECTL_TIMEOUT_RC
    assert "timed out" in out


# ── snapshots: the same seconds knob ────────────────────────────────────────────────────


def test_snapshots_delete_reads_the_same_seconds_knob_as_backups(monkeypatch):
    calls = []
    _record_subprocess_run(monkeypatch, calls)
    config = logic.parse_reap_config({"LONGHORN_REAP_DELETE_TIMEOUT_S": "100"})
    rc, _out = snapshots_mod._delete_snapshot("some-snap", config)
    assert rc == 0
    argv, kwargs = calls[0]
    assert "--timeout=100s" in argv
    assert kwargs["timeout"] == 100 + logic.DELETE_TIMEOUT_MARGIN_S


def test_snapshots_delete_timeout_expired_reports_kubectl_timed_out(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout") or 0.0)

    monkeypatch.setattr(host_lib.subprocess, "run", fake_run)
    rc, out = snapshots_mod._delete_snapshot("some-snap", DEFAULTS)
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
    # The rejecting half. Bash's `select([.status.containerStatuses[].ready] | all)` does not
    # count such a pod as vacuously ready: `.[]` over a null field raises "Cannot iterate over
    # null", jq exits nonzero, and bash takes its "no ready manager pod" branch. A pod whose containers have not started
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
