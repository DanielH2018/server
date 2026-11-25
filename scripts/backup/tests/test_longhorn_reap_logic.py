#!/usr/bin/env python3
"""Tests for longhorn_reap_lib/logic.py, the pure classifier both reapers share.

Every floor gets an `..._is_clean` / `..._is_flagged` pair per CLAUDE.md's red-proof rule: one
input the floor must keep, one it must reap. The FLOOR 1 case additionally covers a FLOOR 1 that is
inoperative because kubectl jsonpath cannot iterate a label MAP: nothing else catches it,
because the dry run's own "0 reapable" output reads as success.

Run: uv run pytest scripts/backup/tests/test_longhorn_reap_logic.py
"""

import pytest

from backup.longhorn_reap_lib import logic
from _reap_entrypoint_harness import _backup, _lh_volume, _snapshot


# ── helpers ──────────────────────────────────────────────────────────────────────────────


def _recurringjob(name, groups):
    return {"metadata": {"name": name}, "spec": {"groups": groups}}


# ── backup_owner_map / existing_volume_set ──────────────────────────────────────────────


def test_backup_owner_map_reads_the_group_from_the_label_keys():
    owner = logic.backup_owner_map([_lh_volume("pihole-etc", group="weekly-backup-d3")])
    assert owner == {"pihole-etc": "weekly-backup-d3"}


def test_backup_owner_map_is_empty_for_a_volume_with_no_group_label():
    assert logic.backup_owner_map([_lh_volume("orphan-vol", group=None)]) == {}


def test_existing_volume_set_lists_every_volume_by_name():
    assert logic.existing_volume_set([_lh_volume("a"), _lh_volume("b")]) == {"a", "b"}


# ── abort_reason ─────────────────────────────────────────────────────────────────────────


def test_abort_reason_is_none_when_ownership_resolves():
    assert logic.abort_reason(volume_count=3, owner_count=3) is None


def test_abort_reason_fires_when_volumes_exist_but_no_owner_resolved():
    reason = logic.abort_reason(volume_count=22, owner_count=0)
    assert reason is not None
    assert "ABORT" in reason
    assert "22 volume" in reason


def test_abort_reason_is_clean_with_no_volumes_and_no_backups():
    # The only honestly empty cluster: nothing to read, nothing at risk.
    assert logic.abort_reason(volume_count=0, owner_count=0, backup_count=0) is None


def test_abort_reason_is_flagged_with_no_volumes_but_backups_that_exist():
    # A zero-item volume read orphans every labelled backup, and --apply-deleted-volumes then
    # deletes the whole B2 set.
    reason = logic.abort_reason(volume_count=0, owner_count=0, backup_count=47)
    assert reason is not None
    assert "ABORT" in reason
    assert "47 backup" in reason


def test_abort_reason_is_clean_when_every_group_resolves_to_a_job():
    assert logic.abort_reason(unresolved_owner_count=0) is None


def test_abort_reason_is_flagged_when_a_group_label_resolves_to_no_job():
    reason = logic.abort_reason(
        volume_count=9,
        owner_count=9,
        recurringjob_count=4,
        volumes_with_group_label=9,
        unresolved_owner_count=3,
    )
    assert reason is not None
    assert "ABORT" in reason
    assert "3 volume" in reason


def test_unresolved_owner_count_counts_only_the_volumes_that_resolved_to_nothing():
    owner = {"a": "weekly-backup", "b": "", "c": "daily-backup", "d": ""}
    assert logic.unresolved_owner_count(owner) == 2


def test_abort_reason_fires_when_the_recurringjob_list_is_empty_but_volumes_carry_the_label():
    # snapshot_owner_map records "" for a labelled volume whose group has no job, so owner_count
    # alone passes.
    reason = logic.abort_reason(
        volume_count=3, owner_count=3, recurringjob_count=0, volumes_with_group_label=3
    )
    assert reason is not None
    assert "ABORT" in reason
    assert "3 volume" in reason


def test_abort_reason_is_clean_when_recurringjobs_resolve_normally():
    assert (
        logic.abort_reason(
            volume_count=3,
            owner_count=3,
            recurringjob_count=2,
            volumes_with_group_label=3,
        )
        is None
    )


def test_abort_reason_skips_the_recurringjob_check_for_the_backups_reaper():
    assert logic.abort_reason(volume_count=3, owner_count=3) is None


def test_volumes_with_group_label_counts_only_labelled_volumes():
    volumes = [_lh_volume("a", group="weekly-backup-d3"), _lh_volume("b", group=None)]
    assert logic.volumes_with_group_label(volumes) == 1


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
    """A hand-triggered probe backup must not count as tier evidence.

    The jsonpath bug's effect was an EMPTY ownership map (OWNER_COUNT==0 for every volume), so
    `owner.get(vol, "")` returned "" for wg-easy-config specifically -- not a mismatched real
    group name. A fixture with a POPULATED owner map (e.g. `{"wg-easy-config":
    "weekly-backup-d3"}`) cannot exercise the danger: an empty probe JOB never equals a real
    group name either way, so a regression that dropped the `if not job: continue` skip would
    still pass a populated-owner fixture by coincidence. This one uses owner={}, matching what
    the jsonpath bug actually produced, so the probe's job=="" DOES equal owner.get(vol,"")=="" --
    which is exactly the condition the skip has to guard against.
    """
    owner: dict[
        str, str
    ] = {}  # wg-easy-config resolves to no owner, as the jsonpath bug did
    backups = [
        _backup("probe", "wg-easy-config", "2026-08-19T00:00:00Z", job=""),
        _backup(
            "stray-newest", "wg-easy-config", "2026-08-16T00:00:00Z", "daily-backup"
        ),
        _backup("stray-b", "wg-easy-config", "2026-08-15T00:00:00Z", "daily-backup"),
        _backup("stray-c", "wg-easy-config", "2026-08-14T00:00:00Z", "daily-backup"),
        _backup(
            "stray-oldest", "wg-easy-config", "2026-08-13T00:00:00Z", "daily-backup"
        ),
    ]
    result = logic.classify_backups(backups, owner, existing_volumes={"wg-easy-config"})

    assert result.candidates == []
    assert "probe" not in {name for name, *_ in result.kept}
    assert "probe" not in {name for name, *_ in result.candidates}
    kept = {name for name, *_ in result.kept}
    assert kept == {"stray-newest", "stray-b", "stray-c", "stray-oldest"}


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
    owner: dict[str, str] = {}  # the volume is gone, so it resolves to no owner
    backups = [_backup("stray", "gone-vol", "2026-08-14T00:00:00Z", "daily-backup")]
    result = logic.classify_backups(backups, owner, existing_volumes={"live-vol"})
    assert result.candidates == []
    assert [n for n, *_ in result.orphaned] == ["stray"]


def test_a_backup_with_an_empty_volumename_is_skipped_entirely():
    result = logic.classify_backups(
        [_backup("weird", "", "2026-08-14T00:00:00Z", "daily-backup")],
        owner={},
        existing_volumes=set(),
    )
    assert result.candidates == result.kept == result.orphaned == []


def test_a_backup_with_a_real_volumename_still_classifies_normally():
    result = logic.classify_backups(
        [_backup("stray", "gone-vol", "2026-08-14T00:00:00Z", "daily-backup")],
        owner={},
        existing_volumes={"live-vol"},
    )
    assert [n for n, *_ in result.orphaned] == ["stray"]


def test_classify_backups_is_clean_when_one_volume_of_several_was_deleted():
    # The case the guard below must NOT swallow: a real deleted volume, correctly orphaned.
    backups = [
        _backup("stray", "gone-vol", "2026-08-14T00:00:00Z", "daily-backup"),
        _backup("current", "live-vol", "2026-08-14T00:00:00Z", "daily-backup"),
    ]
    result = logic.classify_backups(
        backups, {"live-vol": "daily-backup"}, existing_volumes={"live-vol"}
    )
    assert [n for n, *_ in result.orphaned] == ["stray"]


def test_classify_backups_is_flagged_when_the_volume_list_is_empty():
    backups = [
        _backup("b1", "vol-a", "2026-08-14T00:00:00Z", "daily-backup"),
        _backup("b2", "vol-b", "2026-08-14T00:00:00Z", "daily-backup"),
    ]
    with pytest.raises(logic.ReapAbort) as excinfo:
        logic.classify_backups(backups, {}, existing_volumes=set())
    assert "ABORT" in str(excinfo.value)
    assert "2 backup" in str(excinfo.value)


def test_classify_backups_is_clean_when_an_empty_volume_list_has_no_backups_to_lose():
    # An empty cluster reads empty on both sides, so there is nothing the guard protects.
    assert logic.classify_backups([], {}, existing_volumes=set()).orphaned == []


def test_a_stray_with_an_empty_snapshotcreatedat_is_kept_not_reaped():
    owner = {"vol-a": "weekly-backup-d3"}
    backups = [
        _backup("current-1", "vol-a", "2026-08-20T00:00:00Z", "weekly-backup-d3"),
        _backup("stray-real", "vol-a", "2026-08-17T00:00:00Z", "daily-backup"),
        _backup("stray-unknown-age", "vol-a", "", "daily-backup"),
    ]
    result = logic.classify_backups(backups, owner, existing_volumes={"vol-a"})
    assert "stray-unknown-age" not in {n for n, *_ in result.candidates}
    kept = {n for n, _vol, _reason in result.kept}
    assert "stray-unknown-age" in kept
    # the real-timestamped stray still claims the FLOOR 2 floor as before
    assert "stray-real" in kept


def test_creation_order_decides_the_floor_not_listing_order():
    owner = {"vol-a": "weekly-backup-d3"}
    backups = [
        _backup("current-1", "vol-a", "2026-08-20T00:00:00Z", "weekly-backup-d3"),
        _backup("stray-1", "vol-a", "2026-08-16T23:00:00Z", "daily-backup"),
        _backup("stray-2", "vol-a", "2026-08-16T22:30:00-02:00", "daily-backup"),
    ]
    forward = logic.classify_backups(backups, owner, existing_volumes={"vol-a"})
    backward = logic.classify_backups(
        list(reversed(backups)), owner, existing_volumes={"vol-a"}
    )
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


def test_a_snapshot_with_an_empty_volume_is_skipped_entirely():
    result = logic.classify_snapshots(
        [_snapshot("weird", "", "2026-08-01T00:00:00Z", job="daily-backup")],
        owner={},
        attached={""},
        min_age_days=3,
        now_epoch=_NOW,
    )
    assert result.candidates == result.kept == []


def test_the_newest_snapshot_is_never_a_candidate_whoever_made_it():
    owner = {}
    snaps = [_snapshot("newest", "vol-a", "2026-08-19T00:00:00Z")]
    result = logic.classify_snapshots(
        snaps, owner, attached={"vol-a"}, min_age_days=3, now_epoch=_NOW
    )
    assert result.candidates == []
    assert result.kept == []  # not even reported — it's excluded before any floor runs


def test_a_stranded_snapshot_past_the_age_floor_is_flagged():
    # An absent key, not "": "" means the group label named a nonexistent RecurringJob, which is
    # refused.
    owner: dict[str, str] = {}
    snaps = [
        _snapshot("newest", "vol-a", "2026-08-19T00:00:00Z"),
        _snapshot("stale", "vol-a", "2026-08-10T00:00:00Z", job="daily-backup"),
    ]
    result = logic.classify_snapshots(
        snaps, owner, attached={"vol-a"}, min_age_days=3, now_epoch=_NOW
    )
    assert [n for n, *_ in result.candidates] == ["stale"]


def test_a_stranded_snapshot_younger_than_the_age_floor_is_kept():
    # An absent key, not "": "" means the group label named a nonexistent RecurringJob, which is
    # refused.
    owner: dict[str, str] = {}
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
    # A delete on a detached volume does not stick: no running engine coalesces it.
    # An absent key, not "": "" means the group label named a nonexistent RecurringJob, which is
    # refused.
    owner: dict[str, str] = {}
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
    # An absent key, not "": "" means the group label named a nonexistent RecurringJob, which is
    # refused.
    owner: dict[str, str] = {}
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


def test_a_removed_newest_snapshot_does_not_consume_the_floor_slot():
    # An already-removed newest record must not take the FLOOR 1 slot from the newest live snapshot.
    owner: dict[str, str] = {}
    snaps = [
        _snapshot("removed-newest", "vol-a", "2026-08-19T00:00:00Z", removed=True),
        _snapshot("live-newest", "vol-a", "2026-08-10T00:00:00Z", job="daily-backup"),
    ]
    result = logic.classify_snapshots(
        snaps, owner, attached={"vol-a"}, min_age_days=3, now_epoch=_NOW
    )
    assert result.candidates == []
    assert result.kept == []  # live-newest claims FLOOR 1, not even reported


@pytest.mark.parametrize("removed", [None, False], ids=["absent", "false"])
def test_an_unpopulated_markremoved_is_treated_as_not_removed(removed):
    # status.markRemoved is absent on a snapshot read moments after creation.
    owner: dict[str, str] = {}
    stale = _snapshot("stale", "vol-a", "2026-08-10T00:00:00Z", "daily-backup", removed)
    snaps = [_snapshot("newest", "vol-a", "2026-08-19T00:00:00Z"), stale]
    result = logic.classify_snapshots(
        snaps, owner, attached={"vol-a"}, min_age_days=3, now_epoch=_NOW
    )
    assert [n for n, *_ in result.candidates] == ["stale"]


def test_a_hand_taken_snapshot_with_no_job_label_is_never_a_candidate():
    # An absent key, not "": "" means the group label named a nonexistent RecurringJob, which is
    # refused.
    owner: dict[str, str] = {}
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
    # Longhorn truncates weekly-backup-d3 to `weekly-backup` in the snapshot label; the match must
    # be a prefix, not equality.
    group_job = logic.recurringjob_group_to_job(
        [_recurringjob("weekly-backup", ["weekly-backup-d3"])]
    )
    owner = logic.snapshot_owner_map(
        [_lh_volume("vol-a", group="weekly-backup-d3")], group_job
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


def test_classify_snapshots_is_clean_when_every_group_label_resolved_to_a_job():
    group_job = logic.recurringjob_group_to_job(
        [
            _recurringjob("weekly-backup", ["weekly-backup-d3"]),
            _recurringjob("daily-backup", ["daily-backup"]),
        ]
    )
    owner = logic.snapshot_owner_map(
        [
            _lh_volume("vol-a", group="weekly-backup-d3"),
            _lh_volume("vol-b", group="daily-backup"),
        ],
        group_job,
    )
    snaps = [
        _snapshot("newest", "vol-a", "2026-08-19T00:00:00Z"),
        _snapshot("stale", "vol-a", "2026-08-10T00:00:00Z", job="daily-backup"),
    ]
    result = logic.classify_snapshots(
        snaps, owner, attached={"vol-a"}, min_age_days=3, now_epoch=_NOW
    )
    assert [n for n, *_ in result.candidates] == ["stale"]


def test_classify_snapshots_is_flagged_when_one_recurringjob_cr_is_missing():
    # A renamed or deleted RecurringJob resolves the group to "" while the job count stays nonzero,
    # so the all-empty guard stays silent.
    group_job = logic.recurringjob_group_to_job(
        [_recurringjob("daily-backup", ["daily-backup"])]
    )
    owner = logic.snapshot_owner_map(
        [
            _lh_volume("vol-a", group="weekly-backup-d3"),
            _lh_volume("vol-b", group="daily-backup"),
        ],
        group_job,
    )
    assert owner == {"vol-a": "", "vol-b": "daily-backup"}
    snaps = [
        _snapshot("newest", "vol-a", "2026-08-19T00:00:00Z"),
        _snapshot("current", "vol-a", "2026-08-10T00:00:00Z", job="weekly-backup"),
    ]
    with pytest.raises(logic.ReapAbort) as excinfo:
        logic.classify_snapshots(
            snaps, owner, attached={"vol-a"}, min_age_days=3, now_epoch=_NOW
        )
    assert "ABORT" in str(excinfo.value)
    assert "1 volume" in str(excinfo.value)


def test_an_unparseable_creation_time_is_kept_and_reported():
    # An absent key, not "": "" means the group label named a nonexistent RecurringJob, which is
    # refused.
    owner: dict[str, str] = {}
    snaps = [
        _snapshot("newest", "vol-a", "2026-08-19T00:00:00Z"),
        # Lexically below "newest" so the real newest sorts first and this one reaches the age
        # floor.
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
