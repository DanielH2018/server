#!/usr/bin/env python3
"""The backups reaper reads the native trim's per-job label as no tier.

A volume joins Longhorn's `trim` RecurringJob with the per-job label
`recurring-job.longhorn.io/trim`, never with a `recurring-job-group.longhorn.io/trim` group
label. `backup_owner_map` reads only group keys today, so a default-tier volume that carries
the trim label keeps `default` as its owner. A reader widened to per-job keys, or an arm that
writes the group form, would make `trim` the owner, and `classify_backups` would then see no
current-tier backup and keep every stray under FLOOR 1.

Each fixture writes its labels in sorted key order, the order kubectl's JSON emits, because
`LonghornVolume.group` keeps the last group key it meets. The red half's `trim` therefore sorts
after `default` and wins, which is what lets it fail.

The cases sit beside `test_longhorn_reap_logic.py` rather than in it, because that module is
allowlisted at its line count in `tests/repo/module_length_allowlist.txt`. The snapshot
reaper's case is in `test_longhorn_reap_opt_out_snapshots.py`.

Run: uv run pytest scripts/backup/tests/test_longhorn_reap_trim_label.py
"""

from backup.longhorn_reap_lib import logic
from lib.k8s_objects import LonghornVolume
from lib.longhorn_lib.longhorn_backups import GROUP_LABEL_PREFIX

from _reap_entrypoint_harness import _backup

GROUP = GROUP_LABEL_PREFIX
TRIM_JOB_LABEL = "recurring-job.longhorn.io/trim"


def _labelled_volume(name: str, labels: list[str]) -> LonghornVolume:
    """A Volume CR whose labels are `labels`, written in sorted key order as kubectl emits them."""
    return LonghornVolume.from_item(
        {
            "metadata": {
                "name": name,
                "labels": dict.fromkeys(sorted(labels), "enabled"),
            },
            "status": {"state": "attached"},
        }
    )


def _backups() -> list[dict]:
    """One current `default` backup and two older strays from a tier the volume left."""
    return [
        _backup("current", "app-data", "2026-08-20T00:00:00Z", "default"),
        _backup("stray-new", "app-data", "2026-08-17T00:00:00Z", "weekly-backup-d3"),
        _backup("stray-old", "app-data", "2026-08-10T00:00:00Z", "weekly-backup-d3"),
    ]


def test_a_default_volume_with_the_trim_label_keeps_default_as_its_owner():
    volume = _labelled_volume("app-data", [GROUP + "default", TRIM_JOB_LABEL])
    owner = logic.backup_owner_map([volume])
    assert owner == {"app-data": "default"}

    result = logic.classify_backups(_backups(), owner, existing_volumes={"app-data"})
    assert [n for n, *_ in result.candidates] == ["stray-old"]
    assert result.kept == [("stray-new", "app-data", "newest stray, kept as a floor")]


def test_a_trim_group_label_is_misread_as_the_volumes_tier():
    """Red half: the group form makes `trim` the owner, and the current backups stop counting."""
    volume = _labelled_volume("app-data", [GROUP + "default", GROUP + "trim"])
    owner = logic.backup_owner_map([volume])
    assert owner == {"app-data": "trim"}

    result = logic.classify_backups(_backups(), owner, existing_volumes={"app-data"})
    assert result.candidates == []
    assert {reason for *_, reason in result.kept} == {
        "current tier has produced no backup"
    }
