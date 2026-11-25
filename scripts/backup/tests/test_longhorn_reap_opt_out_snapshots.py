#!/usr/bin/env python3
"""The snapshot reaper against a recurring-job group that has no RecurringJob by design.

The `no-backup` group is an opt-out: `k8s/roles/longhorn/templates/storageclass-nobackup.yaml.j2`
selects it so Longhorn's default-group job stops claiming the volume, and no RecurringJob ever
claims it. `snapshot_owner_map` must not resolve it to `""` — the same value a renamed
RecurringJob leaves — or the unresolved-owner ABORT refuses every run on a cluster where
volumes carry that label.

The pair here is the opt-out floor: a run with opt-out volumes proceeds, and a genuinely missing
RecurringJob beside one still refuses. `tests/longhorn/test_longhorn_reap_opt_out_groups.py`
pins the group list itself to the StorageClasses that assign it.

The last pair holds the native trim's per-job label (target slice H2.2a) as no tier: an opt-out
volume that carries it keeps its sentinel, and the group form, its red half, loses it.

Run: uv run pytest scripts/backup/tests/test_longhorn_reap_opt_out_snapshots.py
"""

import pytest

from backup.longhorn_reap_lib import logic
from lib.k8s_objects import LonghornVolume
from lib.longhorn_lib.longhorn_backups import GROUP_LABEL_PREFIX

from test_longhorn_reap_logic import _NOW, _lh_volume, _recurringjob, _snapshot


def test_snapshot_owner_map_is_clean_for_an_opt_out_group_with_no_recurringjob():
    # The `no-backup` group has no RecurringJob by design; the sentinel is a third owner state that
    # `unresolved_owner_count` must not count.
    group_job = logic.recurringjob_group_to_job(
        [_recurringjob("daily-backup", ["default"])]
    )
    owner = logic.snapshot_owner_map(
        [
            _lh_volume("otel-loki", group="no-backup"),
            _lh_volume("vol-b", group="default"),
        ],
        group_job,
    )
    assert owner == {
        "otel-loki": logic.OWNER_NO_JOB_BY_DESIGN,
        "vol-b": "daily-backup",
    }
    assert logic.unresolved_owner_count(owner) == 0
    snaps = [_snapshot("newest", "otel-loki", "2026-08-19T00:00:00Z")]
    result = logic.classify_snapshots(
        snaps, owner, attached={"otel-loki"}, min_age_days=3, now_epoch=_NOW
    )
    assert result.candidates == []
    assert result.kept == []


def test_classify_snapshots_is_flagged_for_a_missing_job_even_beside_an_opt_out_volume():
    group_job = logic.recurringjob_group_to_job(
        [_recurringjob("daily-backup", ["default"])]
    )
    owner = logic.snapshot_owner_map(
        [
            _lh_volume("otel-loki", group="no-backup"),
            _lh_volume("vol-a", group="weekly-backup-d3"),
        ],
        group_job,
    )
    assert owner == {
        "otel-loki": logic.OWNER_NO_JOB_BY_DESIGN,
        "vol-a": "",
    }
    snaps = [
        _snapshot("newest", "vol-a", "2026-08-19T00:00:00Z"),
        _snapshot("current", "vol-a", "2026-08-10T00:00:00Z", job="weekly-backup"),
    ]
    with pytest.raises(logic.ReapAbort) as excinfo:
        logic.classify_snapshots(
            snaps, owner, attached={"vol-a"}, min_age_days=3, now_epoch=_NOW
        )
    assert "1 volume" in str(excinfo.value)


def test_a_recurring_job_snapshot_on_an_opt_out_volume_is_reaped_but_its_newest_is_kept():
    group_job = logic.recurringjob_group_to_job(
        [_recurringjob("daily-backup", ["default"])]
    )
    owner = logic.snapshot_owner_map(
        [_lh_volume("otel-loki", group="no-backup")], group_job
    )
    snaps = [
        _snapshot("newest", "otel-loki", "2026-08-19T00:00:00Z", job="daily-backup"),
        _snapshot("stray", "otel-loki", "2026-08-01T00:00:00Z", job="daily-backup"),
    ]
    result = logic.classify_snapshots(
        snaps, owner, attached={"otel-loki"}, min_age_days=3, now_epoch=_NOW
    )
    assert [n for n, *_ in result.candidates] == ["stray"]
    assert result.kept == []


TRIM_JOB_LABEL = "recurring-job.longhorn.io/trim"


def _trim_volume(name, group_keys):
    # Sorted key order, as kubectl emits: `LonghornVolume.group` keeps the last group key it meets.
    labels = dict.fromkeys(sorted(group_keys), "enabled")
    return LonghornVolume.from_item(
        {"metadata": {"name": name, "labels": labels}, "status": {"state": "attached"}}
    )


def test_an_opt_out_volume_with_the_trim_label_keeps_its_sentinel():
    group_job = logic.recurringjob_group_to_job(
        [_recurringjob("daily-backup", ["default"]), _recurringjob("trim", [])]
    )
    volume = _trim_volume(
        "otel-loki", [GROUP_LABEL_PREFIX + "no-backup", TRIM_JOB_LABEL]
    )
    owner = logic.snapshot_owner_map([volume], group_job)
    assert owner == {"otel-loki": logic.OWNER_NO_JOB_BY_DESIGN}
    assert logic.unresolved_owner_count(owner) == 0


def test_a_trim_group_label_costs_an_opt_out_volume_its_sentinel():
    group_job = logic.recurringjob_group_to_job(
        [_recurringjob("daily-backup", ["default"]), _recurringjob("trim", [])]
    )
    volume = _trim_volume(
        "otel-loki",
        [GROUP_LABEL_PREFIX + g for g in ("no-backup", "trim")],
    )
    owner = logic.snapshot_owner_map([volume], group_job)
    assert owner == {"otel-loki": ""}
    assert logic.unresolved_owner_count(owner) == 1
