#!/usr/bin/env python3
"""Tests for longhorn_reap_logic.py, the pure classifier both reap-orphan entry points share.

Every floor gets an `..._is_clean` / `..._is_flagged` pair per CLAUDE.md's red-proof rule: one
input the floor must keep, one it must reap. The FLOOR 1 case additionally reproduces the
2026-08-16 incident from test_longhorn_reap_guard.py's original docstring: FLOOR 1 shipped
inoperative because kubectl jsonpath cannot iterate a label MAP, and nothing caught it because
the dry run's own "0 reapable" output read as success.

Run: uv run pytest ansible/roles/setup/k3s/tests/test_longhorn_reap_logic.py
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "files"))
import longhorn_reap_logic as logic


# ── helpers ──────────────────────────────────────────────────────────────────────────────


def _volume(name, *, group=None, state="attached"):
    labels = {}
    if group is not None:
        labels["recurring-job-group.longhorn.io/%s" % group] = "enabled"
    return {"metadata": {"name": name, "labels": labels}, "status": {"state": state}}


def _backup(name, vol, created, job, state="Completed"):
    return {
        "metadata": {"name": name},
        "status": {
            "volumeName": vol,
            "snapshotCreatedAt": created,
            "labels": {"RecurringJob": job} if job else {},
            "state": state,
        },
    }


def _snapshot(name, vol, created, job=None, removed=None):
    status = {"creationTime": created}
    if job is not None:
        status["labels"] = {"RecurringJob": job}
    if removed is not None:
        status["markRemoved"] = removed
    return {"metadata": {"name": name}, "spec": {"volume": vol}, "status": status}


def _recurringjob(name, groups):
    return {"metadata": {"name": name}, "spec": {"groups": groups}}


# ── backup_owner_map / existing_volume_set ──────────────────────────────────────────────


def test_backup_owner_map_reads_the_group_from_the_label_keys():
    owner = logic.backup_owner_map([_volume("pihole-etc", group="weekly-backup-d3")])
    assert owner == {"pihole-etc": "weekly-backup-d3"}


def test_backup_owner_map_is_empty_for_a_volume_with_no_group_label():
    assert logic.backup_owner_map([_volume("orphan-vol", group=None)]) == {}


def test_existing_volume_set_lists_every_volume_by_name():
    assert logic.existing_volume_set([_volume("a"), _volume("b")]) == {"a", "b"}


# ── abort_reason ─────────────────────────────────────────────────────────────────────────


def test_abort_reason_is_none_when_ownership_resolves():
    assert logic.abort_reason(volume_count=3, owner_count=3) is None


def test_abort_reason_fires_when_volumes_exist_but_no_owner_resolved():
    # This is the assertion that would have caught the original jsonpath defect: the lookup can
    # break in ways a fixture cannot anticipate, so the script has to notice the RESULT is
    # unusable rather than trust that an empty map means "nothing is stranded".
    reason = logic.abort_reason(volume_count=22, owner_count=0)
    assert reason is not None
    assert "ABORT" in reason
    assert "22 volume" in reason


def test_abort_reason_is_none_with_no_volumes_at_all():
    assert logic.abort_reason(volume_count=0, owner_count=0) is None


# ── resolve_kubeconfig ───────────────────────────────────────────────────────────────────


def test_resolve_kubeconfig_uses_readonly_when_no_delete_is_requested():
    path, err = logic.resolve_kubeconfig(
        needs_admin=False,
        admin_readable=False,
        admin_path="/etc/rancher/k3s/k3s.yaml",
        readonly_path="/home/ubuntu/.kube/config",
        sudo_hint="sudo x --apply",
    )
    assert err is None
    assert path == "/home/ubuntu/.kube/config"


def test_resolve_kubeconfig_refuses_a_delete_run_without_the_admin_kubeconfig():
    # --apply used to run under the read-only ServiceAccount, so every delete came back
    # Forbidden; with no return-code check the loop ran to completion and reported success
    # having deleted nothing. Refusing before any kubectl call is the fix.
    path, err = logic.resolve_kubeconfig(
        needs_admin=True,
        admin_readable=False,
        admin_path="/etc/rancher/k3s/k3s.yaml",
        readonly_path="/home/ubuntu/.kube/config",
        sudo_hint="sudo x --apply",
    )
    assert path is None
    assert err is not None
    assert "/etc/rancher/k3s/k3s.yaml" in err
    assert "sudo x --apply" in err


def test_resolve_kubeconfig_uses_admin_when_readable_and_needed():
    path, err = logic.resolve_kubeconfig(
        needs_admin=True,
        admin_readable=True,
        admin_path="/etc/rancher/k3s/k3s.yaml",
        readonly_path="/home/ubuntu/.kube/config",
        sudo_hint="sudo x --apply",
    )
    assert err is None
    assert path == "/etc/rancher/k3s/k3s.yaml"


# ── classify_backups: FLOOR 1 (current tier has produced nothing) ──────────────────────────


def test_floor1_is_flagged_when_current_tier_has_produced_backups_past_the_floor():
    # A volume whose current tier already has one backup and carries three daily-era strays:
    # the newest stray is kept as FLOOR 2, the other two are reapable.
    owner = {"vol-a": "weekly-backup-d3"}
    backups = [
        _backup("current-1", "vol-a", "2026-08-20T00:00:00Z", "weekly-backup-d3"),
        _backup("stray-3", "vol-a", "2026-08-18T00:00:00Z", "daily-backup"),
        _backup("stray-2", "vol-a", "2026-08-17T00:00:00Z", "daily-backup"),
        _backup("stray-1", "vol-a", "2026-08-16T00:00:00Z", "daily-backup"),
    ]
    result = logic.classify_backups(backups, owner, existing_volumes={"vol-a"})
    reaped = {name for name, *_ in result.candidates}
    kept = {name for name, _vol, _reason in result.kept}
    assert reaped == {"stray-2", "stray-1"}
    assert "stray-3" in kept  # newest stray, kept as a floor


def test_floor1_is_clean_when_current_tier_has_produced_nothing():
    # The 2026-08-16 incident: wg-easy-config's tier had produced zero backups of its own, so
    # every one of its daily-era strays is the entire recovery this volume has. None reapable.
    owner = {"wg-easy-config": "weekly-backup-d3"}
    backups = [
        _backup("stray-5", "wg-easy-config", "2026-08-16T00:00:00Z", "daily-backup"),
        _backup("stray-4", "wg-easy-config", "2026-08-15T00:00:00Z", "daily-backup"),
        _backup("stray-3", "wg-easy-config", "2026-08-14T00:00:00Z", "daily-backup"),
        _backup("stray-2", "wg-easy-config", "2026-08-13T00:00:00Z", "daily-backup"),
        _backup("stray-1", "wg-easy-config", "2026-08-12T00:00:00Z", "daily-backup"),
    ]
    result = logic.classify_backups(backups, owner, existing_volumes={"wg-easy-config"})
    assert result.candidates == []
    assert {name for name, *_ in result.kept} == {
        "stray-5",
        "stray-4",
        "stray-3",
        "stray-2",
        "stray-1",
    }


def test_a_hand_triggered_probe_backup_cannot_stand_in_as_tier_evidence():
    # wg-easy-config's recorded 3-of-5 case: one hand-triggered probe backup (no RecurringJob
    # label) must not count toward CURRENT_TIER_COUNT and must never itself be a candidate.
    # Without the exclusion, that single probe backup satisfies `JOB == OWNER[$VOL]` under the
    # empty-string comparison and disarms FLOOR 1 for every real stray behind it.
    owner = {"wg-easy-config": "weekly-backup-d3"}
    backups = [
        _backup("probe", "wg-easy-config", "2026-08-19T00:00:00Z", job=""),
        _backup("stray-2", "wg-easy-config", "2026-08-15T00:00:00Z", "daily-backup"),
        _backup("stray-1", "wg-easy-config", "2026-08-14T00:00:00Z", "daily-backup"),
    ]
    result = logic.classify_backups(backups, owner, existing_volumes={"wg-easy-config"})
    assert result.candidates == []
    assert "probe" not in {name for name, *_ in result.kept}
    assert "probe" not in {name for name, *_ in result.candidates}
    kept = {name for name, *_ in result.kept}
    assert kept == {"stray-2", "stray-1"}  # both kept: tier has produced no real backup


def test_non_completed_backups_are_never_bucketed():
    owner = {"vol-a": "daily-backup"}
    backups = [
        _backup(
            "in-progress",
            "vol-a",
            "2026-08-20T00:00:00Z",
            "daily-backup",
            state="InProgress",
        )
    ]
    result = logic.classify_backups(backups, owner, existing_volumes={"vol-a"})
    assert result.candidates == result.kept == result.orphaned == []


def test_backups_of_a_deleted_volume_land_in_orphaned_not_candidates():
    owner = {}  # the volume is gone, so it resolves to no owner
    backups = [_backup("stray", "gone-vol", "2026-08-14T00:00:00Z", "daily-backup")]
    result = logic.classify_backups(backups, owner, existing_volumes=set())
    assert result.candidates == []
    assert [n for n, *_ in result.orphaned] == ["stray"]


def test_creation_order_decides_the_floor_not_listing_order():
    owner = {"vol-a": "weekly-backup-d3"}
    backups = [
        _backup("current-1", "vol-a", "2026-08-20T00:00:00Z", "weekly-backup-d3"),
        _backup("stray-1", "vol-a", "2026-08-16T00:00:00Z", "daily-backup"),
        _backup("stray-2", "vol-a", "2026-08-17T00:00:00Z", "daily-backup"),
    ]
    forward = logic.classify_backups(backups, owner, existing_volumes={"vol-a"})
    backward = logic.classify_backups(
        list(reversed(backups)), owner, existing_volumes={"vol-a"}
    )
    # stray-2 (08-17) is the newer stray, so it is kept as the FLOOR 2 floor and stray-1
    # (08-16) is the sole candidate — regardless of the order the two arrived in.
    assert (
        {n for n, *_ in forward.kept} == {n for n, *_ in backward.kept} == {"stray-2"}
    )
    assert (
        [n for n, *_ in forward.candidates]
        == [n for n, *_ in backward.candidates]
        == ["stray-1"]
    )


# ── classify_snapshots ───────────────────────────────────────────────────────────────────

_now = logic.parse_rfc3339_epoch("2026-08-20T00:00:00Z")
assert _now is not None
_NOW: float = _now


def test_the_newest_snapshot_is_never_a_candidate_whoever_made_it():
    owner = {}
    snaps = [_snapshot("newest", "vol-a", "2026-08-19T00:00:00Z")]
    result = logic.classify_snapshots(
        snaps, owner, attached={"vol-a"}, min_age_days=3, now_epoch=_NOW
    )
    assert result.candidates == []
    assert result.kept == []  # not even reported — it's excluded before any floor runs


def test_a_stranded_snapshot_past_the_age_floor_is_flagged():
    owner = {"vol-a": ""}
    snaps = [
        _snapshot("newest", "vol-a", "2026-08-19T00:00:00Z"),
        _snapshot("stale", "vol-a", "2026-08-10T00:00:00Z", job="daily-backup"),
    ]
    result = logic.classify_snapshots(
        snaps, owner, attached={"vol-a"}, min_age_days=3, now_epoch=_NOW
    )
    assert [n for n, *_ in result.candidates] == ["stale"]


def test_a_stranded_snapshot_younger_than_the_age_floor_is_kept():
    owner = {"vol-a": ""}
    snaps = [
        _snapshot("newest", "vol-a", "2026-08-19T18:00:00Z"),
        _snapshot("recent", "vol-a", "2026-08-19T00:00:00Z", job="daily-backup"),
    ]
    result = logic.classify_snapshots(
        snaps, owner, attached={"vol-a"}, min_age_days=3, now_epoch=_NOW
    )
    assert result.candidates == []
    reasons = {name: reason for name, _vol, reason in result.kept}
    assert "younger than 3d" in reasons["recent"]


def test_a_detached_volumes_snapshot_is_kept_not_reaped():
    # Deleting a snapshot on a detached volume does not stick: there is no running engine to
    # coalesce it, so a delete-then-recreate loop is churn that reads as progress.
    owner = {"vol-a": ""}
    snaps = [
        _snapshot("newest", "vol-a", "2026-08-19T00:00:00Z"),
        _snapshot("stale", "vol-a", "2026-08-10T00:00:00Z", job="daily-backup"),
    ]
    result = logic.classify_snapshots(
        snaps, owner, attached=set(), min_age_days=3, now_epoch=_NOW
    )
    assert result.candidates == []
    reasons = {name: reason for name, _vol, reason in result.kept}
    assert "detached" in reasons["stale"]


def test_an_already_removed_snapshot_is_skipped_silently_not_reaped_again():
    owner = {"vol-a": ""}
    snaps = [
        _snapshot("newest", "vol-a", "2026-08-19T00:00:00Z"),
        _snapshot(
            "gone", "vol-a", "2026-08-10T00:00:00Z", job="daily-backup", removed=True
        ),
    ]
    result = logic.classify_snapshots(
        snaps, owner, attached={"vol-a"}, min_age_days=3, now_epoch=_NOW
    )
    assert result.candidates == []
    assert result.kept == []


def test_an_unpopulated_markremoved_is_treated_as_not_removed():
    # R14, ported: status.markRemoved absent (a snapshot read moments after creation) must be
    # treated the same as an explicit False, not silently excluded.
    owner = {"vol-a": ""}
    snaps = [
        _snapshot("newest", "vol-a", "2026-08-19T00:00:00Z"),
        _snapshot(
            "stale", "vol-a", "2026-08-10T00:00:00Z", job="daily-backup"
        ),  # no `removed` key
    ]
    result = logic.classify_snapshots(
        snaps, owner, attached={"vol-a"}, min_age_days=3, now_epoch=_NOW
    )
    assert [n for n, *_ in result.candidates] == ["stale"]


def test_a_hand_taken_snapshot_with_no_job_label_is_never_a_candidate():
    owner = {"vol-a": ""}
    snaps = [
        _snapshot("newest", "vol-a", "2026-08-19T00:00:00Z"),
        _snapshot("by-hand", "vol-a", "2026-08-10T00:00:00Z", job=None),
    ]
    result = logic.classify_snapshots(
        snaps, owner, attached={"vol-a"}, min_age_days=3, now_epoch=_NOW
    )
    assert result.candidates == []
    assert result.kept == []


def test_the_truncated_job_name_prefix_match_keeps_a_weekly_shards_current_snapshot():
    # Longhorn truncates weekly-backup-d3 to `weekly-backup` in the snapshot's own label. An
    # equality test would report every weekly-tier volume's CURRENT snapshot as stranded;
    # FLOOR 1 only protects the newest, so the second-newest would become a false candidate.
    group_job = logic.recurringjob_group_to_job(
        [_recurringjob("weekly-backup", ["weekly-backup-d3"])]
    )
    owner = logic.snapshot_owner_map(
        [_volume("vol-a", group="weekly-backup-d3")], group_job
    )
    assert owner == {"vol-a": "weekly-backup"}
    snaps = [
        _snapshot("newest", "vol-a", "2026-08-19T00:00:00Z"),
        _snapshot(
            "second-newest-current",
            "vol-a",
            "2026-08-12T00:00:00Z",
            job="weekly-backup",
        ),
    ]
    result = logic.classify_snapshots(
        snaps, owner, attached={"vol-a"}, min_age_days=3, now_epoch=_NOW
    )
    assert result.candidates == []
    assert result.kept == []


def test_a_stranded_snapshot_from_a_since_moved_shard_is_kept_not_reaped():
    # A `weekly-backup` snapshot whose owning job is now a DIFFERENT shard reads as current
    # under the prefix rule and is kept — the shard that made it isn't recoverable from the
    # label anyway, so this is the deliberately-wrong-direction floor.
    owner = {
        "vol-a": "weekly-backup"
    }  # owner resolves to the truncated form too, by construction
    snaps = [
        _snapshot("newest", "vol-a", "2026-08-19T00:00:00Z"),
        _snapshot("moved-shard", "vol-a", "2026-08-10T00:00:00Z", job="weekly-backup"),
    ]
    result = logic.classify_snapshots(
        snaps, owner, attached={"vol-a"}, min_age_days=3, now_epoch=_NOW
    )
    assert result.candidates == []


def test_an_unparseable_creation_time_is_kept_and_reported():
    owner = {"vol-a": ""}
    snaps = [
        _snapshot("newest", "vol-a", "2026-08-19T00:00:00Z"),
        # Lexically less than "newest"'s stamp (same date prefix, malformed suffix) so the
        # sort-by-created-descending step still puts the real newest first — an unparseable
        # stamp that happened to sort first would hit FLOOR 1 and never reach this floor at all.
        _snapshot("bad-time", "vol-a", "2026-08-05Tgarbage", job="daily-backup"),
    ]
    result = logic.classify_snapshots(
        snaps, owner, attached={"vol-a"}, min_age_days=3, now_epoch=_NOW
    )
    assert result.candidates == []
    reasons = {name: reason for name, _vol, reason in result.kept}
    assert "unparseable creationTime" in reasons["bad-time"]


def test_recurringjob_group_to_job_maps_every_group_a_job_declares():
    mapping = logic.recurringjob_group_to_job(
        [_recurringjob("weekly-backup", ["weekly-backup-d0", "weekly-backup-d3"])]
    )
    assert mapping == {
        "weekly-backup-d0": "weekly-backup",
        "weekly-backup-d3": "weekly-backup",
    }
