#!/usr/bin/env python3
"""Longhorn Backup CRs as records, and the backup group and target names, defined once.

WHY ONE READER (#3735). The backup-health cron, the two reapers and probe.py each fetched
`backups.longhorn.io` and pulled the same `.status` fields out by hand, through `-o json` in
some and pipe- or space-delimited jsonpath in others. The health cron alone fetched the list
four times a run. `from_items` is that parsing done once, over the one `-o json` body every
reader already handles; the field names and the `Completed` filter live here.

WHY THE NAMES LIVE HERE TOO (#3737). The RecurringJob group labels and the BackupTarget names
are a contract with the cluster: `tasks/longhorn.yml` labels each volume with a group, and the
readers select on it. Renaming a live group label drops its volumes out of their RecurringJob
with nothing failing, so the Python readers take each name from here rather than retyping it.
The Ansible tasks and the static StorageClass file still spell the labels out; issue #3737's
follow-up covers that half.

WHERE IT RUNS. The health cron imports it as a sibling in /opt/longhorn-backup-health/, so it
is copied there beside `host_lib.py` (`tasks/health-crons.yml`). The reapers and probe.py run
from the repo checkout and put this directory on `sys.path` (`lib.repo_paths.K3S_FILES`). It
must stay importable on the host Python floor, with no import beyond the standard library and
`host_lib`.

Typical usage example:

    backups = longhorn_backups.from_items(json.loads(out)["items"])
    longhorn_backups.completed(backups)
"""

from __future__ import annotations

from dataclasses import dataclass

import host_lib

RESOURCE = "backups.longhorn.io"
LIST_ARGS = ("get", RESOURCE, "-o", "json")

# A volume joins a RecurringJob group through a `<prefix><group>=enabled` label. `default` is
# Longhorn's implicit group, covering every volume with no group of its own; `no-backup` is the
# opt-out group and names no RecurringJob; `weekly-backup` is the pre-shard weekly group, still
# read as a tier. The weekly tier has one shard per weekday, so no day's deletions reach the B2
# transaction cap.
GROUP_LABEL_PREFIX = "recurring-job-group.longhorn.io/"
DEFAULT_GROUP = "default"
NO_BACKUP_GROUP = "no-backup"
WEEKLY_LEGACY_GROUP = "weekly-backup"
WEEKLY_SHARDS = 7

# The BackupTarget names. Longhorn ships a target called `default`, and
# `tasks/longhorn-backup.yml` points it at B2, so a reader that means "B2" says B2_TARGET rather
# than the word `default`.
B2_TARGET = "default"
R2_TARGET = "r2"

COMPLETED = "Completed"


WEEKLY_SHARD_PREFIX = "weekly-backup-d"


def weekly_shard_group(shard: int) -> str:
    """The group of weekly shard `shard`, 0 to WEEKLY_SHARDS - 1."""
    return f"{WEEKLY_SHARD_PREFIX}{shard}"


def group_label(group: str) -> str:
    """The volume label key that puts a volume in `group`."""
    return GROUP_LABEL_PREFIX + group


@dataclass(frozen=True)
class Backup:
    """One Backup CR, its `.status` fields as Longhorn wrote them.

    A missing field is the empty string, as a jsonpath read would print it.

    Attributes:
        name: `.metadata.name`.
        volume: `.status.volumeName`.
        created: `.status.snapshotCreatedAt`, raw. Longhorn writes it with no format
            guarantee, so `created_epoch` is the parsed form and this is what a sort sees.
        state: `.status.state`, such as `Completed`, `InProgress` or `Error`.
        job: `.status.labels.RecurringJob`, the job that produced it; empty for a manual one.
        size: `.status.size`, a decimal byte count as a string.
    """

    name: str
    volume: str
    created: str
    state: str
    job: str
    size: str

    @property
    def created_epoch(self) -> float | None:
        """`created` in seconds since the epoch, or None when it does not parse."""
        return host_lib.rfc3339_to_epoch(self.created)


def from_item(item: dict) -> Backup:
    """One `.items` entry of `kubectl get backups.longhorn.io -o json` as a `Backup`."""
    status = item.get("status") or {}
    return Backup(
        name=str((item.get("metadata") or {}).get("name") or ""),
        volume=str(status.get("volumeName") or ""),
        created=str(status.get("snapshotCreatedAt") or ""),
        state=str(status.get("state") or ""),
        job=str((status.get("labels") or {}).get("RecurringJob") or ""),
        size=str(status.get("size") or ""),
    )


def from_items(items: list[dict]) -> list[Backup]:
    """Every entry of a backup list's `.items`, in the order kubectl returned them."""
    return [from_item(item) for item in items]


def completed(backups: list[Backup]) -> list[Backup]:
    """The backups Longhorn finished, which are the only ones a restore can use."""
    return [b for b in backups if b.state == COMPLETED]
