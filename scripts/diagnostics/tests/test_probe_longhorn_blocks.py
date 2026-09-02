"""Tests for `probe.py longhorn-blocks`.

The census exists because `longhorn-16mi-migration-state` was a hand-written snapshot of which
volumes carry which block size, and a snapshot decays. A live query cannot.

Each rule is an accept/reject pair. The rejecting half matters more than usual here: the check's
whole job is to notice a weekly-shard volume that is NOT on 16 MiB, and a census that only ever
prints rows would look identical to one that asserts nothing.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import probe_longhorn as ps

_GROUP = "recurring-job-group.longhorn.io/"
_16MiB = str(16 * 1024 * 1024)
_2MiB = str(2 * 1024 * 1024)


def _vol(name, group, target="default", block=_16MiB):
    labels = {f"{_GROUP}{group}": "enabled"} if group else {}
    return {
        "metadata": {"name": name, "labels": labels},
        "spec": {"backupTargetName": target, "backupBlockSize": block},
    }


def _items(*vols):
    return {"items": list(vols)}


def test_the_group_decides_the_tier_not_the_target():
    """`default` is the DEFAULT target name, so an unbacked volume reports it too.

    Grouping by target alone reads the no-backup volumes as B2-tier members — measured
    2026-08-30, that is 18 of them on this cluster.
    """
    rows = ps.volume_tier_census(
        _items(
            _vol("a", "weekly-backup-d0"),
            _vol("b", "no-backup", block=_2MiB),
        )
    )
    assert ("weekly-backup-d0", "default", _16MiB) in rows
    assert ("no-backup", "default", _2MiB) in rows


def test_a_weekly_volume_off_the_block_size_is_flagged():
    rows = ps.volume_tier_census(_items(_vol("stale", "weekly-backup-d3", block=_2MiB)))
    offenders = ps.weekly_volumes_off_block_size(rows)
    assert offenders and "stale" in offenders[0] and "weekly-backup-d3" in offenders[0]


def test_a_weekly_volume_on_the_block_size_is_clean():
    rows = ps.volume_tier_census(_items(_vol("good", "weekly-backup-d3")))
    assert ps.weekly_volumes_off_block_size(rows) == []


def test_a_no_backup_volume_at_2MiB_is_not_flagged():
    """Nothing backs it up, so its block size cannot cost anything."""
    rows = ps.volume_tier_census(_items(_vol("scratch", "no-backup", block=_2MiB)))
    assert ps.weekly_volumes_off_block_size(rows) == []


def test_an_r2_daily_volume_at_2MiB_is_not_flagged():
    """The recorded exception: immutable in place and not worth recreating."""
    rows = ps.volume_tier_census(
        _items(_vol("daily", "default", target="r2", block=_2MiB))
    )
    assert ps.weekly_volumes_off_block_size(rows) == []


def test_a_volume_with_no_group_label_is_not_flagged():
    rows = ps.volume_tier_census(_items(_vol("orphan", None, block=_2MiB)))
    assert ps.weekly_volumes_off_block_size(rows) == []


def test_the_formatter_fails_and_names_the_offender():
    rows = ps.volume_tier_census(_items(_vol("stale", "weekly-backup-d3", block=_2MiB)))
    text, code = ps.format_block_census(rows)
    assert code == 1
    assert "FAIL" in text and "stale" in text
    assert "migrate_volume_block_size.yml" in text, (
        "the fix must be named, not just the fault"
    )


def test_the_formatter_passes_on_a_clean_estate():
    rows = ps.volume_tier_census(
        _items(_vol("a", "weekly-backup-d0"), _vol("b", "no-backup", block=_2MiB))
    )
    text, code = ps.format_block_census(rows)
    assert code == 0 and "OK:" in text


def test_an_empty_estate_does_not_read_as_healthy_by_accident():
    """No volumes means nothing was measured. It passes, but the census must show it is empty.

    Recorded rather than fixed: this probe is run by hand and its output is read, unlike a
    monitor. An empty census is visibly empty.
    """
    text, code = ps.format_block_census(ps.volume_tier_census({"items": []}))
    assert code == 0
    assert "OK:" in text
    assert "count=" not in text
