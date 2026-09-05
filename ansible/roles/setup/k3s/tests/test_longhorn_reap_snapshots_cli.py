#!/usr/bin/env python3
"""The snapshots reaper's CLI, run as a real subprocess: dry run, ownership refusals, --apply.

Split out of `test_longhorn_reap_entrypoints.py`, which keeps the bootstrap and the fail-closed
arms. What these pin is the I/O shell around `classify_snapshots`: that a dry run never calls
`kubectl delete`, that a snapshot whose owning recurring job cannot be resolved aborts rather
than being reaped, and that --apply emits exactly the delete argv the classifier chose. The
decisions themselves are covered against fixtures in `test_longhorn_reap_logic.py`.

The stub `k3s` and the staging harness are shared in `_reap_entrypoint_harness.py`.

Run: uv run pytest ansible/roles/setup/k3s/tests/test_longhorn_reap_snapshots_cli.py
"""

from _reap_entrypoint_harness import SNAPSHOTS_ENTRY, _run, _snapshot, _volume


# ── snapshots: dry run emits no delete ──────────────────────────────────────────────────


def test_snapshots_dry_run_emits_no_delete_call(tmp_path):
    fixtures = {
        "recurringjobs": [
            {"metadata": {"name": "daily-backup"}, "spec": {"groups": ["daily-backup"]}}
        ],
        "volumes": [_volume("vol-a", "daily-backup")],
        "snapshots": [
            _snapshot("newest", "vol-a", "2026-08-19T00:00:00Z"),
            _snapshot("stale", "vol-a", "2026-08-01T00:00:00Z", job="weekly-backup"),
        ],
    }
    proc, calls = _run(SNAPSHOTS_ENTRY, [], fixtures, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "dry run" in proc.stdout
    assert not any("delete" in c for c in calls)


def test_snapshots_aborts_when_recurringjobs_is_empty_but_a_volume_carries_the_label(
    tmp_path,
):
    # Verified without this check: 3 current-tier snapshots deleted on a dry run. An empty
    # recurringjobs.longhorn.io list makes every group resolve to "", which the snapshot's own
    # truncated job label ("daily-backup") never equals -- so the "current, not stranded" skip
    # never fires and every current-tier snapshot past the newest reads as stranded.
    fixtures = {
        "recurringjobs": [],  # the RecurringJob CRs failed to list, or none exist
        "volumes": [_volume("vol-a", "daily-backup")],
        "snapshots": [
            _snapshot("newest", "vol-a", "2026-08-19T00:00:00Z"),
            _snapshot("current-2", "vol-a", "2026-08-18T00:00:00Z", job="daily-backup"),
            _snapshot("current-3", "vol-a", "2026-08-17T00:00:00Z", job="daily-backup"),
        ],
    }
    proc, calls = _run(SNAPSHOTS_ENTRY, [], fixtures, tmp_path)
    assert proc.returncode == 1
    assert "ABORT" in proc.stderr
    assert "reapable" not in proc.stdout  # never gets far enough to print a verdict
    # ABORT fires right after reading recurringjobs + volumes; snapshots.longhorn.io is never
    # read and no delete is ever attempted.
    # Exact match, not `in`: see ansible/tests/repo/test_no_host_shaped_membership_literal.py
    assert not any(tok == "snapshots.longhorn.io" for c in calls for tok in c)
    assert not any("delete" in c for c in calls)


def test_snapshots_refuse_when_a_labelled_volume_resolves_to_no_job(tmp_path):
    # The rejecting half for #1063: RecurringJobs list fine, but the group this volume is
    # labelled with names no job, so its owner resolves to "" and the current-tier test is False
    # against every snapshot it has. classify_snapshots raises ReapAbort; main() must print the
    # ABORT line and exit 1 rather than let the traceback out.
    fixtures = {
        "recurringjobs": [
            {"metadata": {"name": "daily-backup"}, "spec": {"groups": ["daily-backup"]}}
        ],
        "volumes": [_volume("vol-a", "weekly-backup-d3")],
        "snapshots": [
            _snapshot("newest", "vol-a", "2026-08-19T00:00:00Z"),
            _snapshot("older", "vol-a", "2026-08-01T00:00:00Z", job="weekly-backup-d3"),
        ],
    }
    proc, calls = _run(SNAPSHOTS_ENTRY, [], fixtures, tmp_path)
    assert proc.returncode == 1
    assert "ABORT" in proc.stderr
    assert "Traceback" not in proc.stderr, proc.stderr
    assert not any("delete" in c for c in calls)


def test_snapshots_dry_run_prints_all_four_columns_for_a_reapable_row(tmp_path):
    fixtures = {
        "recurringjobs": [
            {"metadata": {"name": "daily-backup"}, "spec": {"groups": ["daily-backup"]}}
        ],
        "volumes": [_volume("vol-a", "daily-backup")],
        "snapshots": [
            _snapshot("newest", "vol-a", "2026-08-19T00:00:00Z"),
            _snapshot("stale", "vol-a", "2026-08-01T00:00:00Z", job="weekly-backup"),
        ],
    }
    proc, _calls = _run(SNAPSHOTS_ENTRY, [], fixtures, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "stale vol-a 2026-08-01T00:00:00Z weekly-backup" in proc.stdout


def test_snapshots_dry_run_prints_the_age_floor_as_a_bare_integer_day_count(tmp_path):
    # k3s_longhorn_snapshot_reap_min_age_days is an int in defaults/main.yml; bash's arithmetic
    # context only ever held one, and printed "younger than 3d" -- not "3.0d", which a
    # float-typed MIN_AGE_DAYS would print instead.
    import datetime

    one_day_ago = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    fixtures = {
        # A resolvable RecurringJob so `abort_reason`'s recurringjob check does not fire; the
        # "recent" snapshot's own job ("daily-backup") deliberately does not match what the
        # volume's group resolves to ("some-job"), so it reaches the age floor instead of being
        # skipped as current.
        "recurringjobs": [
            {"metadata": {"name": "some-job"}, "spec": {"groups": ["some-group"]}}
        ],
        "volumes": [_volume("vol-a", "some-group")],
        "snapshots": [
            _snapshot("newest", "vol-a", one_day_ago),
            _snapshot("recent", "vol-a", one_day_ago, job="daily-backup"),
        ],
    }
    proc, _calls = _run(SNAPSHOTS_ENTRY, [], fixtures, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "younger than 3d" in proc.stdout
    assert "3.0d" not in proc.stdout


def test_snapshots_dry_run_refuses_when_readonly_kubeconfig_is_unset(tmp_path):
    proc, calls = _run(
        SNAPSHOTS_ENTRY,
        [],
        {"recurringjobs": [], "volumes": []},
        tmp_path,
        readonly_kubeconfig_set=False,
    )
    assert proc.returncode == 1
    assert "LONGHORN_REAP_READONLY_KUBECONFIG is not set" in proc.stderr
    assert calls == []


def test_snapshots_apply_without_admin_kubeconfig_refuses_and_deletes_nothing(tmp_path):
    fixtures = {"recurringjobs": [], "volumes": [], "snapshots": []}
    proc, calls = _run(SNAPSHOTS_ENTRY, ["--apply"], fixtures, tmp_path)
    assert proc.returncode == 1
    assert "admin kubeconfig" in proc.stderr
    assert calls == []


def test_snapshots_apply_emits_exactly_the_delete_argv_the_classifier_chose(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LONGHORN_REAP_MIN_AGE_DAYS", "3")
    fixtures = {
        "recurringjobs": [
            {"metadata": {"name": "daily-backup"}, "spec": {"groups": ["daily-backup"]}}
        ],
        "volumes": [_volume("vol-a", "daily-backup")],
        "snapshots": [
            _snapshot("newest", "vol-a", "2026-08-19T00:00:00Z"),
            _snapshot("stale", "vol-a", "2026-08-01T00:00:00Z", job="weekly-backup"),
        ],
        # A pod on a DIFFERENT node than this process's hostname, so the purge step finds no
        # ready manager pod locally and the run reports that rather than hanging on a real
        # connection — same "no ready backend" branch the module docstring explains.
        "pods": [
            {
                "spec": {"nodeName": "not-this-host"},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [{"ready": True}],
                    "podIP": "127.0.0.1",
                },
            }
        ],
    }
    proc, calls = _run(
        SNAPSHOTS_ENTRY, ["--apply"], fixtures, tmp_path, admin_readable=True
    )
    deletes = [c for c in calls if "delete" in c]
    assert deletes == [
        [
            "kubectl",
            "-n",
            "longhorn-system",
            "delete",
            "snapshots.longhorn.io",
            "stale",
            "--ignore-not-found",
            "--timeout=120s",
        ]
    ]
    # The delete itself succeeded; the run still fails closed because no ready manager pod
    # was found on this node, so the purge that reclaims the freed blocks never ran.
    assert proc.returncode == 1
    assert "NOT purged" in proc.stderr


def test_snapshots_unknown_flag_is_rejected(tmp_path):
    proc, calls = _run(
        SNAPSHOTS_ENTRY, ["--bogus"], {"recurringjobs": [], "volumes": []}, tmp_path
    )
    assert proc.returncode == 2
    assert calls == []
