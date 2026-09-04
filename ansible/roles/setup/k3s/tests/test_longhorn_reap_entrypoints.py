#!/usr/bin/env python3
"""The reap-orphan entry points' bootstrap, and every way they must fail closed.

Two subjects, both about the entry point rather than about either subcommand's output. The
bootstrap pair proves `sys.path.insert(0, dirname(__file__)); import host_lib` resolves when the
script is invoked directly, which is how it runs in production (uv run
<path>/longhorn_reap_orphan_backups.py). The fail-closed arms prove an unreadable input — a
`null` JSON body from kubectl, a non-integral env knob — produces a named ABORT rather than a
traceback, and never a delete.

The per-subcommand CLI behaviour is in `test_longhorn_reap_backups_cli.py` and
`test_longhorn_reap_snapshots_cli.py`; everything decision-shaped is covered against fixtures in
`test_longhorn_reap_logic.py`. All three CLI suites run through
`_reap_entrypoint_harness.py`.

Run: uv run pytest ansible/roles/setup/k3s/tests/test_longhorn_reap_entrypoints.py
"""

from __future__ import annotations

from _reap_entrypoint_harness import (
    BACKUPS_ENTRY,
    SNAPSHOTS_ENTRY,
    _run,
    _snapshot,
    _volume,
)


# ── sys.path bootstrap ───────────────────────────────────────────────────────────────────


def test_backups_entrypoint_resolves_its_sibling_imports_when_run_directly(tmp_path):
    proc, _calls = _run(BACKUPS_ENTRY, [], {"volumes": []}, tmp_path)
    assert "ModuleNotFoundError" not in proc.stderr, proc.stderr


def test_snapshots_entrypoint_resolves_its_sibling_imports_when_run_directly(tmp_path):
    proc, _calls = _run(SNAPSHOTS_ENTRY, [], {"volumes": []}, tmp_path)
    assert "ModuleNotFoundError" not in proc.stderr, proc.stderr


# ── null JSON bodies: ABORT, not a traceback ────────────────────────────────────────────


def test_backups_aborts_cleanly_when_the_volume_list_body_is_null(tmp_path):
    # A well-formed but non-object body (`kubectl` emitting a bare `null`) used to reach
    # `.get("items", [])` and raise AttributeError -- a traceback where every other unreadable
    # read here prints ABORT. See longhorn_reap_logic.parse_kubectl_json_items.
    proc, calls = _run(
        BACKUPS_ENTRY, [], {"volumes": []}, tmp_path, null_kinds=["volumes"]
    )
    assert proc.returncode == 1
    assert "ABORT: unparseable volume list" in proc.stderr
    assert "Traceback" not in proc.stderr, proc.stderr
    assert not any("delete" in c for c in calls)


def test_backups_aborts_cleanly_when_the_backup_list_body_is_null(tmp_path):
    fixtures = {"volumes": [_volume("vol-a", "daily-backup")], "backups": []}
    proc, calls = _run(BACKUPS_ENTRY, [], fixtures, tmp_path, null_kinds=["backups"])
    assert proc.returncode == 1
    assert "ABORT: unparseable backup list" in proc.stderr
    assert "Traceback" not in proc.stderr, proc.stderr
    assert not any("delete" in c for c in calls)


def test_snapshots_aborts_cleanly_when_the_recurringjob_list_body_is_null(tmp_path):
    proc, calls = _run(
        SNAPSHOTS_ENTRY,
        [],
        {"recurringjobs": [], "volumes": []},
        tmp_path,
        null_kinds=["recurringjobs"],
    )
    assert proc.returncode == 1
    assert "ABORT: unparseable RecurringJob list" in proc.stderr
    assert "Traceback" not in proc.stderr, proc.stderr
    assert not any("delete" in c for c in calls)


def test_snapshots_aborts_cleanly_when_the_snapshot_list_body_is_null(tmp_path):
    fixtures = {
        "recurringjobs": [
            {"metadata": {"name": "daily-backup"}, "spec": {"groups": ["daily-backup"]}}
        ],
        "volumes": [_volume("vol-a", "daily-backup")],
        "snapshots": [],
    }
    proc, calls = _run(
        SNAPSHOTS_ENTRY, [], fixtures, tmp_path, null_kinds=["snapshots"]
    )
    assert proc.returncode == 1
    assert "ABORT: unparseable snapshot list" in proc.stderr
    assert "Traceback" not in proc.stderr, proc.stderr
    assert not any("delete" in c for c in calls)


def test_snapshots_purge_warns_cleanly_when_the_pod_list_body_is_null(tmp_path):
    # The pods read is inside _purge, WARNING-prefixed rather than ABORT-prefixed -- a
    # different call site than the other three, so it gets its own case.
    fixtures = {
        "recurringjobs": [
            {"metadata": {"name": "daily-backup"}, "spec": {"groups": ["daily-backup"]}}
        ],
        "volumes": [_volume("vol-a", "daily-backup")],
        "snapshots": [
            _snapshot("newest", "vol-a", "2026-08-19T00:00:00Z"),
            _snapshot("stale", "vol-a", "2026-08-01T00:00:00Z", job="weekly-backup"),
        ],
        "pods": [],
    }
    proc, calls = _run(
        SNAPSHOTS_ENTRY,
        ["--apply"],
        fixtures,
        tmp_path,
        admin_readable=True,
        null_kinds=["pods"],
    )
    assert proc.returncode == 1
    assert "WARNING: unparseable pod list" in proc.stderr
    assert "nothing purged" in proc.stderr
    assert "Traceback" not in proc.stderr, proc.stderr
    # the snapshot delete itself still ran; only the purge read hit the null body
    assert any("delete" in c for c in calls)


# ── non-integral env knobs: a named ABORT, not a traceback ─────────────────────────────


def test_snapshots_aborts_cleanly_on_a_non_integral_min_age_days(tmp_path):
    # k3s_longhorn_snapshot_reap_min_age_days is an int in defaults/main.yml, but a host_vars
    # override or a typo could set a non-integral value; `int(os.environ.get(...))` at module
    # scope used to raise before main() could print a named ABORT.
    proc, calls = _run(
        SNAPSHOTS_ENTRY,
        [],
        {"recurringjobs": [], "volumes": []},
        tmp_path,
        extra_env={"LONGHORN_REAP_MIN_AGE_DAYS": "3.5"},
    )
    assert proc.returncode == 2
    assert "LONGHORN_REAP_MIN_AGE_DAYS expects an integer, got: 3.5" in proc.stderr
    assert "Traceback" not in proc.stderr, proc.stderr
    assert calls == []


def test_snapshots_aborts_cleanly_on_a_non_integral_kubectl_timeout(tmp_path):
    proc, calls = _run(
        SNAPSHOTS_ENTRY,
        [],
        {"recurringjobs": [], "volumes": []},
        tmp_path,
        extra_env={"LONGHORN_REAP_KUBECTL_TIMEOUT_S": "30.5"},
    )
    assert proc.returncode == 2
    assert (
        "LONGHORN_REAP_KUBECTL_TIMEOUT_S expects an integer, got: 30.5" in proc.stderr
    )
    assert "Traceback" not in proc.stderr, proc.stderr
    assert calls == []
