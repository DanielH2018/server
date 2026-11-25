#!/usr/bin/env python3
"""The backups reaper's CLI, run as a real subprocess: dry run, --apply, and the deletion cap.

Split out of `test_longhorn_reap_entrypoints.py`, which keeps the bootstrap and the fail-closed
arms. What these pin is the I/O shell around `classify_backups`: that a dry run never calls
`kubectl delete`, that --apply emits exactly the delete argv the classifier chose, that
--apply-deleted-volumes is the only path to the orphaned bucket, and that the --max-deletions
cap refuses before deleting anything. The decisions themselves are covered against fixtures in
`test_longhorn_reap_logic.py`.

The stub `k3s` and the staging harness are shared in `_reap_entrypoint_harness.py`.

Run: uv run pytest scripts/backup/tests/test_longhorn_reap_backups_cli.py
"""

from _reap_entrypoint_harness import (
    BACKUPS_ENTRY,
    _backup,
    _delete_names,
    _run,
    _volume,
)


# ── backups: dry run emits no delete ────────────────────────────────────────────────────


def test_backups_dry_run_emits_no_delete_call(tmp_path):
    fixtures = {
        "volumes": [_volume("vol-a", "daily-backup")],
        "backups": [
            _backup("current-1", "vol-a", "2026-08-20T00:00:00Z", "daily-backup"),
            _backup("stray-1", "vol-a", "2026-08-14T00:00:00Z", "weekly-backup"),
        ],
    }
    proc, calls = _run(BACKUPS_ENTRY, [], fixtures, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "dry run" in proc.stdout
    assert not any("delete" in c for c in calls)


def test_backups_dry_run_prints_all_four_columns_for_a_reapable_row(tmp_path):
    fixtures = {
        "volumes": [_volume("vol-a", "daily-backup")],
        "backups": [
            _backup("current-1", "vol-a", "2026-08-20T00:00:00Z", "daily-backup"),
            _backup(
                "stray-2", "vol-a", "2026-08-15T00:00:00Z", "weekly-backup"
            ),  # kept, FLOOR 2
            _backup(
                "stray-1", "vol-a", "2026-08-14T00:00:00Z", "weekly-backup"
            ),  # reapable
        ],
    }
    proc, _calls = _run(BACKUPS_ENTRY, [], fixtures, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "stray-1 vol-a 2026-08-14T00:00:00Z weekly-backup" in proc.stdout


def test_backups_dry_run_refuses_when_readonly_kubeconfig_is_unset(tmp_path):
    # An unset readonly kubeconfig must not fall through to the caller's KUBECONFIG, which as root
    # is the admin one.
    proc, calls = _run(
        BACKUPS_ENTRY, [], {"volumes": []}, tmp_path, readonly_kubeconfig_set=False
    )
    assert proc.returncode == 1
    assert "LONGHORN_REAP_READONLY_KUBECONFIG is not set" in proc.stderr
    assert calls == []


def test_backups_apply_without_admin_kubeconfig_refuses_and_deletes_nothing(tmp_path):
    fixtures = {"volumes": [], "backups": []}
    proc, calls = _run(BACKUPS_ENTRY, ["--apply"], fixtures, tmp_path)
    assert proc.returncode == 1
    assert "admin kubeconfig" in proc.stderr
    assert calls == []


def test_backups_unknown_flag_is_rejected(tmp_path):
    proc, calls = _run(BACKUPS_ENTRY, ["--bogus"], {"volumes": []}, tmp_path)
    assert proc.returncode == 64
    assert calls == []


def test_backups_abort_on_unresolvable_ownership_makes_no_delete_call(tmp_path):
    fixtures = {
        "volumes": [{"metadata": {"name": "vol-a", "labels": {}}, "status": {}}],
        "backups": [],
    }
    proc, calls = _run(BACKUPS_ENTRY, [], fixtures, tmp_path)
    assert proc.returncode == 1
    assert "ABORT" in proc.stderr
    assert not any("delete" in c for c in calls)
    assert not any(tok == "backups.longhorn.io" for c in calls for tok in c)


def test_backups_apply_emits_exactly_the_delete_argv_the_classifier_chose(tmp_path):
    fixtures = {
        "volumes": [_volume("vol-a", "weekly-backup-d3")],
        "backups": [
            _backup("current-1", "vol-a", "2026-08-20T00:00:00Z", "weekly-backup-d3"),
            _backup("stray-2", "vol-a", "2026-08-17T00:00:00Z", "daily-backup"),
            _backup("stray-1", "vol-a", "2026-08-16T00:00:00Z", "daily-backup"),
        ],
    }
    proc, calls = _run(
        BACKUPS_ENTRY, ["--apply"], fixtures, tmp_path, admin_readable=True
    )
    assert proc.returncode == 0, proc.stderr
    deletes = [c for c in calls if "delete" in c]
    # stray-2 is the newer stray and is kept as the FLOOR 2 floor; only stray-1 is reapable.
    assert deletes == [
        [
            "kubectl",
            "-n",
            "longhorn-system",
            "delete",
            "backups.longhorn.io",
            "stray-1",
            "--ignore-not-found",
            "--timeout=120s",
        ]
    ]


def test_backups_apply_stops_at_the_first_failed_delete(tmp_path):
    # A failed delete must stop the whole run, not continue into the rest of the bucket or the next
    # one.
    fixtures = {
        "volumes": [_volume("vol-a", "weekly-backup-d3")],
        "backups": [
            _backup("current-1", "vol-a", "2026-08-20T00:00:00Z", "weekly-backup-d3"),
            _backup(
                "stray-4", "vol-a", "2026-08-19T00:00:00Z", "daily-backup"
            ),  # kept, FLOOR 2
            _backup("stray-3", "vol-a", "2026-08-18T00:00:00Z", "daily-backup"),
            _backup("stray-2", "vol-a", "2026-08-17T00:00:00Z", "daily-backup"),
            _backup("stray-1", "vol-a", "2026-08-16T00:00:00Z", "daily-backup"),
        ],
    }
    proc, calls = _run(
        BACKUPS_ENTRY,
        ["--apply"],
        fixtures,
        tmp_path,
        admin_readable=True,
        fail_delete_names=["stray-2"],
    )
    assert proc.returncode == 1
    deletes = _delete_names(calls)
    # stray-3 attempted and succeeded, stray-2 attempted and failed, stray-1 NEVER attempted.
    assert deletes == ["stray-3", "stray-2"]


def test_backups_apply_deleted_volumes_only_deletes_the_orphaned_bucket(tmp_path):
    fixtures = {
        # An empty volume list means the volume read returned nothing, not that every volume was
        # deleted.
        "volumes": [_volume("live-vol", "daily-backup")],
        "backups": [
            _backup("stray", "gone-vol", "2026-08-14T00:00:00Z", "daily-backup")
        ],
    }
    plain_apply, plain_calls = _run(
        BACKUPS_ENTRY, ["--apply"], fixtures, tmp_path, admin_readable=True
    )
    assert plain_apply.returncode == 0, plain_apply.stderr
    assert not any("delete" in c for c in plain_calls)

    with_flag, calls = _run(
        BACKUPS_ENTRY,
        ["--apply-deleted-volumes"],
        fixtures,
        tmp_path,
        admin_readable=True,
    )
    assert with_flag.returncode == 0, with_flag.stderr
    deletes = [c for c in calls if "delete" in c]
    assert deletes == [
        [
            "kubectl",
            "-n",
            "longhorn-system",
            "delete",
            "backups.longhorn.io",
            "stray",
            "--ignore-not-found",
            "--timeout=120s",
        ]
    ]


def test_backups_apply_deleted_volumes_refuses_on_an_empty_volume_list(tmp_path):
    # A volume read with zero items would orphan every labelled backup; main() must print ABORT and
    # exit 1, not a traceback.
    fixtures = {
        "volumes": [],
        "backups": [
            _backup("b1", "vol-a", "2026-08-14T00:00:00Z", "daily-backup"),
            _backup("b2", "vol-b", "2026-08-15T00:00:00Z", "daily-backup"),
        ],
    }
    proc, calls = _run(
        BACKUPS_ENTRY,
        ["--apply-deleted-volumes"],
        fixtures,
        tmp_path,
        admin_readable=True,
    )
    assert proc.returncode == 1
    assert "ABORT" in proc.stderr
    assert "Traceback" not in proc.stderr, proc.stderr
    assert _delete_names(calls) == []


def test_backups_apply_deleted_volumes_never_runs_after_a_failed_apply(tmp_path):
    # If the stray bucket fails, the orphaned bucket must never be touched.
    fixtures = {
        "volumes": [_volume("vol-a", "daily-backup")],
        "backups": [
            # A current backup so CURRENT_TIER_COUNT > 0 -- otherwise FLOOR 1 keeps every stray.
            _backup("current-1", "vol-a", "2026-08-20T00:00:00Z", "daily-backup"),
            _backup(
                "stray-newest", "vol-a", "2026-08-15T00:00:00Z", "weekly-backup"
            ),  # kept, FLOOR 2
            _backup(
                "stray", "vol-a", "2026-08-14T00:00:00Z", "weekly-backup"
            ),  # the sole candidate
            _backup("orphan", "gone-vol", "2026-08-14T00:00:00Z", "daily-backup"),
        ],
    }
    proc, calls = _run(
        BACKUPS_ENTRY,
        ["--apply", "--apply-deleted-volumes"],
        fixtures,
        tmp_path,
        admin_readable=True,
        fail_delete_names=["stray"],
    )
    assert proc.returncode == 1
    deletes = _delete_names(calls)
    assert deletes == [
        "stray"
    ]  # the "stray" delete failed; "orphan" was never attempted


# ── backups: the deletion cap ───────────────────────────────────────────────────────────


def _three_candidates_and_one_orphan():
    """Fixtures whose classification is exactly 3 reapable strays and 1 orphan.

    stray-4 is the newest stray and is kept by FLOOR 2, so the reapable bucket is stray-3,
    stray-2, stray-1 -- a count the --max-deletions pair below sits either side of.
    """
    return {
        "volumes": [_volume("vol-a", "weekly-backup-d3")],
        "backups": [
            _backup("current-1", "vol-a", "2026-08-20T00:00:00Z", "weekly-backup-d3"),
            _backup("stray-4", "vol-a", "2026-08-19T00:00:00Z", "daily-backup"),
            _backup("stray-3", "vol-a", "2026-08-18T00:00:00Z", "daily-backup"),
            _backup("stray-2", "vol-a", "2026-08-17T00:00:00Z", "daily-backup"),
            _backup("stray-1", "vol-a", "2026-08-16T00:00:00Z", "daily-backup"),
            _backup("orphan", "gone-vol", "2026-08-14T00:00:00Z", "daily-backup"),
        ],
    }


def test_backups_apply_deletes_when_the_candidate_count_equals_the_cap(tmp_path):
    proc, calls = _run(
        BACKUPS_ENTRY,
        ["--apply", "--max-deletions", "3"],
        _three_candidates_and_one_orphan(),
        tmp_path,
        admin_readable=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert _delete_names(calls) == ["stray-3", "stray-2", "stray-1"]


def test_backups_apply_refuses_when_the_candidate_count_exceeds_the_cap(tmp_path):
    proc, calls = _run(
        BACKUPS_ENTRY,
        ["--apply", "--max-deletions=2"],
        _three_candidates_and_one_orphan(),
        tmp_path,
        admin_readable=True,
    )
    assert proc.returncode == 1
    assert _delete_names(calls) == []
    assert "--max-deletions cap of 2" in proc.stderr


def test_backups_cap_counts_both_buckets_together(tmp_path):
    proc, calls = _run(
        BACKUPS_ENTRY,
        ["--apply", "--apply-deleted-volumes", "--max-deletions", "3"],
        _three_candidates_and_one_orphan(),
        tmp_path,
        admin_readable=True,
    )
    assert proc.returncode == 1
    assert _delete_names(calls) == []
    assert "4 deletion(s) requested" in proc.stderr


def test_backups_max_deletions_without_a_count_is_rejected(tmp_path):
    proc, calls = _run(
        BACKUPS_ENTRY, ["--apply", "--max-deletions"], {"volumes": []}, tmp_path
    )
    assert proc.returncode == 64
    assert calls == []


def test_backups_max_deletions_with_a_non_integer_count_is_rejected(tmp_path):
    proc, calls = _run(
        BACKUPS_ENTRY, ["--apply", "--max-deletions", "lots"], {"volumes": []}, tmp_path
    )
    assert proc.returncode == 64
    assert calls == []


def test_backups_dry_run_documents_the_cap(tmp_path):
    proc, _calls = _run(BACKUPS_ENTRY, [], {"volumes": []}, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "--max-deletions" in proc.stdout
