"""Ansible filter plugin rendering the Longhorn RecurringJob group labels from `longhorn_backups`.

`roles/setup/k3s/files/longhorn_backups.py` names the backup groups once for every Python
reader (#3737). The k3s role's label tasks and RecurringJob template used to spell the same
names out, and a one-byte difference renames a live group: its volumes drop out of their
RecurringJob and nothing fails. These filters let the tasks take each name from the module
instead (#3946).

- `backup_group_label`: the volume label key for a group, `recurring-job-group.longhorn.io/<group>`.
- `weekly_backup_group`: the group of weekly shard N, `weekly-backup-d<N>`.
- `weekly_backup_shard`: the shard a weekly-tier volume belongs to, its list index mod 7.

Each raises `ValueError` on a group or shard the module does not define, so a typo fails the
render instead of writing a label no RecurringJob selects. Ansible wraps the `ValueError` in
its own error.

No Ansible import, so the tests call the same functions the playbook runs.
"""

import sys as _sys
from pathlib import Path as _Path

# `longhorn_backups` imports `host_lib` by bare name, because on the host the two are copied
# side by side. Here they live in two roles' `files/`, so both go on the path.
_ROLES = _Path(__file__).resolve().parents[1] / "roles" / "setup"
for _files in (_ROLES / "common" / "files", _ROLES / "k3s" / "files"):
    if str(_files) not in _sys.path:
        _sys.path.insert(0, str(_files))

import longhorn_backups  # noqa: E402

_SHARDS = range(longhorn_backups.WEEKLY_SHARDS)
KNOWN_GROUPS = frozenset(
    {
        longhorn_backups.DEFAULT_GROUP,
        longhorn_backups.NO_BACKUP_GROUP,
        longhorn_backups.WEEKLY_LEGACY_GROUP,
        *(longhorn_backups.weekly_shard_group(shard) for shard in _SHARDS),
    }
)


def backup_group_label(group: str) -> str:
    """The label key that puts a volume in `group`, refusing a group the module does not name."""
    if group not in KNOWN_GROUPS:
        raise ValueError(
            f"backup_group_label: {group!r} is not a Longhorn backup group; "
            f"known: {', '.join(sorted(KNOWN_GROUPS))}"
        )
    return longhorn_backups.group_label(group)


def weekly_backup_group(shard: int) -> str:
    """The group name of weekly shard `shard`, refusing a shard outside 0 to 6."""
    if int(shard) not in _SHARDS:
        raise ValueError(
            f"weekly_backup_group: shard {shard!r} is outside 0-{len(_SHARDS) - 1}"
        )
    return longhorn_backups.weekly_shard_group(int(shard))


def weekly_backup_shard(pvc: str, weekly_volumes: list) -> int:
    """The weekday shard of `pvc`: its index in `weekly_volumes` mod the shard count.

    `list.index` takes the FIRST occurrence, which is what the tasks' inline
    `k3s_longhorn_weekly_volumes.index(...) % 7` did. Raises `ValueError` when `pvc` is not
    in the list, as `list.index` does.
    """
    return list(weekly_volumes).index(pvc) % longhorn_backups.WEEKLY_SHARDS


class FilterModule:
    """Registers the Longhorn backup group filters."""

    def filters(self):
        return {
            "backup_group_label": backup_group_label,
            "weekly_backup_group": weekly_backup_group,
            "weekly_backup_shard": weekly_backup_shard,
        }
