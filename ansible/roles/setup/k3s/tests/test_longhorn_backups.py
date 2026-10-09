"""Tests for the shared Backup CR reader and the backup group names (#3735, #3737)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "files"))
import longhorn_backups as backups

ROLE = Path(__file__).resolve().parents[1]


def _item(**status):
    return {"metadata": {"name": "backup-1"}, "status": status}


def test_from_item_reads_every_status_field():
    backup = backups.from_item(
        _item(
            volumeName="pvc-a",
            snapshotCreatedAt="2026-10-01T03:30:00Z",
            state="Completed",
            size="1048576",
            labels={"RecurringJob": "daily-backup"},
        )
    )
    assert backup == backups.Backup(
        name="backup-1",
        volume="pvc-a",
        created="2026-10-01T03:30:00Z",
        state="Completed",
        job="daily-backup",
        size="1048576",
    )


def test_from_item_reads_a_missing_field_as_empty_not_none():
    """A jsonpath read printed "" for a field Longhorn has not written; the records match it."""
    backup = backups.from_item({"status": {"labels": None}})
    assert (backup.name, backup.volume, backup.created, backup.job) == ("", "", "", "")


def test_newest_is_the_latest_instant_not_the_last_string():
    """`04:30+01:00` is 03:30Z: text comparison picks it over 03:45Z, time does not (#3735)."""
    items = [
        _item(snapshotCreatedAt="2026-10-01T04:30:00+01:00"),
        _item(snapshotCreatedAt="2026-10-01T03:45:00.123456Z"),
        _item(snapshotCreatedAt="not a stamp"),
        _item(snapshotCreatedAt=""),
    ]
    newest = backups.newest(backups.from_items(items))
    assert newest is not None and newest.created == "2026-10-01T03:45:00.123456Z"
    assert backups.newest(backups.from_items([_item(snapshotCreatedAt="")])) is None


def test_completed_keeps_only_completed_backups():
    items = [_item(state="Completed"), _item(state="Error"), _item(state="InProgress")]
    assert [b.state for b in backups.completed(backups.from_items(items))] == [
        "Completed"
    ]


def test_the_nobackup_storageclass_names_the_no_backup_group():
    """The static StorageClass cannot call a filter, so it alone still spells the group out."""
    nobackup_class = (
        ROLE / "files" / "longhorn-storageclass-nobackup.yaml"
    ).read_text()
    assert f'"name":"{backups.NO_BACKUP_GROUP}","isGroup":true' in nobackup_class
