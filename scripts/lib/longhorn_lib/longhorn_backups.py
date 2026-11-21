#!/usr/bin/env python3
"""Longhorn Backup CRs as records, and the backup group and target names, defined once.

WHY ONE READER. The backup-health cron, the two reapers and probe.py each fetched
`backups.longhorn.io` and pulled the same `.status` fields out by hand, through `-o json` in
some and pipe- or space-delimited jsonpath in others. The health cron alone fetched the list
four times a run. `from_items` is that parsing done once, over the one `-o json` body every
reader already handles; the field names and the `Completed` filter live here.

WHY THE NAMES LIVE HERE TOO. The RecurringJob group labels and the BackupTarget names
are a contract with the cluster: the `longhorn-reconcile` role's `files/longhorn_reconcile.py`
labels each volume with a group, and the readers select on it. Renaming a live group label drops its volumes out of their RecurringJob
with nothing failing, so the Python readers take each name from here rather than retyping it.
The Ansible tasks, the restore drill and `seed_volume_backup.yml` take them from here too,
through `scripts/renderer/template_filters/longhorn_groups.py`. The static StorageClass file cannot
call a filter and still spells `no-backup` out.

WHERE IT RUNS. The health timer imports it as `lib.longhorn_lib.longhorn_backups`, shipped in
/usr/lib/homelab/longhorn-backup-health/lib/longhorn_lib/ with `host_lib.py` in the directory
above `lib/` (hosts/components/k3s-host-checks). The reapers and probe.py import it under the same
name from the checkout. It
must stay importable on the host Python floor, with no import beyond the standard library and
`host_lib`.

Typical usage example:

    backups = longhorn_backups.from_items(json.loads(out)["items"])
    longhorn_backups.completed(backups)
"""

import json
from dataclasses import dataclass

import sys as _sys
from pathlib import Path as _Path

# In the checkout, `hosts/lib` holds host_lib.py and `scripts/lib` holds this package. On a
# host both sit under the reader's directory, which is already on sys.path, so these inserts
# name directories that do not exist there and change nothing.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[3] / "hosts" / "lib"))

import host_lib

RESOURCE = "backups.longhorn.io"
LIST_ARGS = ("get", RESOURCE, "-o", "json")

# A volume joins a RecurringJob group through a `<prefix><group>=enabled` label. `default` is
# Longhorn's implicit group, covering every volume with no group of its own; `no-backup` is the
# opt-out group and names no RecurringJob. The weekly tier has one shard per weekday, so no
# day's deletions reach the B2 transaction cap.
GROUP_LABEL_PREFIX = "recurring-job-group.longhorn.io/"
DEFAULT_GROUP = "default"
NO_BACKUP_GROUP = "no-backup"
WEEKLY_SHARDS = 7

# The daily RecurringJob, which selects the `default` group. The weekly jobs are named for their
# group (`weekly_shard_group`).
DAILY_JOB = "daily-backup"

# The BackupTarget names. Longhorn ships a target called `default`, and
# the `longhorn-backup` role's `default` BackupTarget points it at B2, so a reader that means "B2" says B2_TARGET rather
# than the word `default`.
B2_TARGET = "default"
R2_TARGET = "r2"

# The Volume annotation longhorn-reconcile stamps with the time a volume joined its tier.
TIER_SINCE_ANNOTATION = "homelab/tier-since"

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
            guarantee, so order stamps with `stamp_order`, never by comparing the text.
        state: `.status.state`, such as `Completed`, `InProgress` or `Error`.
        job: `.status.labels.RecurringJob`, the job that produced it; empty for a manual one.
        size: `.status.size`, a decimal byte count as a string.
        claim: the PVC name inside `.status.labels.KubernetesStatus`; see `_recorded_claim`.
        error: `.status.error`, Longhorn's reason for an `Error` state.
    """

    name: str
    volume: str
    created: str
    state: str
    job: str
    size: str
    claim: str
    error: str

    @property
    def is_completed(self) -> bool:
        """Whether Longhorn finished it, which a restore needs."""
        return self.state == COMPLETED


def _recorded_claim(labels: dict) -> str:
    """The PVC name Longhorn recorded in a Backup's labels, or "" when it recorded none.

    Longhorn writes the PVC into `KubernetesStatus` as a JSON STRING, not as a struct. Parsing
    it and comparing the field keeps `n8n-data` from also matching `n8n-data-something`, which
    a substring search over the string would. A label that is absent, not JSON, or not an
    object reads as "no claim recorded".
    """
    raw = labels.get("KubernetesStatus", "")
    if not isinstance(raw, str) or not raw:
        return ""
    try:
        doc = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(doc, dict):
        return ""
    name = doc.get("pvcName", "")
    return name if isinstance(name, str) else ""


def from_item(item: dict) -> Backup:
    """One Backup object as a `Backup`, from a list's `.items` or a single read."""
    status = item.get("status") or {}
    labels = status.get("labels") or {}
    return Backup(
        name=str((item.get("metadata") or {}).get("name") or ""),
        volume=str(status.get("volumeName") or ""),
        created=str(status.get("snapshotCreatedAt") or ""),
        state=str(status.get("state") or ""),
        job=str(labels.get("RecurringJob") or ""),
        size=str(status.get("size") or ""),
        claim=_recorded_claim(labels),
        error=str(status.get("error") or ""),
    )


def from_items(items: list[dict]) -> list[Backup]:
    """Every entry of a backup list's `.items`, in the order kubectl returned them."""
    return [from_item(item) for item in items]


def completed(backups: list[Backup]) -> list[Backup]:
    """The backups Longhorn finished, which are the only ones a restore can use."""
    return [b for b in backups if b.is_completed]


def stamp_order(stamp: str) -> tuple[bool, float, str]:
    """A sort key that orders RFC3339 stamps by the time they name, latest last.

    Comparing the text is wrong once the formats mix: `04:30:00+01:00` sorts after
    `03:45:00Z` although it is the earlier instant. A stamp that does not parse sorts before
    every one that does, so it never wins a "newest" pick. Ties within one second fall back to
    the text, because the parser drops fractional seconds.
    """
    epoch = host_lib.rfc3339_to_epoch(stamp)
    return (epoch is not None, epoch or 0.0, stamp)


def newest(backups: list[Backup]) -> Backup | None:
    """The backup with the latest `created` time, or None when none carries a stamp."""
    stamped = [b for b in backups if b.created.strip()]
    if not stamped:
        return None
    return max(stamped, key=lambda b: stamp_order(b.created))
