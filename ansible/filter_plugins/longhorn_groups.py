"""Ansible filter plugin rendering the Longhorn RecurringJob group labels from `longhorn_backups`.

`roles/setup/k3s/files/longhorn_lib/longhorn_backups.py` names the backup groups once for every Python
reader (#3737). The k3s role's label tasks and RecurringJob template used to spell the same
names out, and a one-byte difference renames a live group: its volumes drop out of their
RecurringJob and nothing fails. These filters let the tasks take each name from the module
instead (#3946).

- `backup_group_label`: the volume label key for a group, `recurring-job-group.longhorn.io/<group>`.
- `weekly_backup_group`: the group of weekly shard N, `weekly-backup-d<N>`.
- `weekly_backup_shard`: the shard a weekly-tier volume belongs to, as its entry declares it.
- `longhorn_weekly_claims` and `longhorn_nobackup_claims`: the k3s role's weekly and no-backup
  volume sets, derived from each `containers_list` entry's `weekly_backup_claims` and
  `no_backup_claims` the way `tier_backup_claims` derives the R2 set (#4207).
- `longhorn_backup_name`: a bare name by key — the label prefix, a group or a BackupTarget —
  for a template that matches on a name rather than writing one label, such as the restore
  drill's jq selector and `seed_volume_backup.yml`.

DECIDED: a task names a group by its own spelling, `'no-backup' | backup_group_label`, not by
a key. The filter refuses any group the module does not define, so a typo fails the render the
same way a key would, and the task still reads as the label it writes (#3946).

Each raises `ValueError` on a group or shard the module does not define, so a typo fails the
render instead of writing a label no RecurringJob selects. Ansible wraps the `ValueError` in
its own error.

No Ansible import, so the tests call the same functions the playbook runs.
"""

import sys as _sys
from pathlib import Path as _Path

# `longhorn_lib.longhorn_backups` imports `host_lib` by bare name, because on the host the
# package and host_lib.py are copied side by side. Here they live in two roles' `files/`, so
# both go on the path.
_ROLES = _Path(__file__).resolve().parents[1] / "roles" / "setup"
for _files in (_ROLES / "common" / "files", _ROLES / "k3s" / "files"):
    if str(_files) not in _sys.path:
        _sys.path.insert(0, str(_files))

from longhorn_lib import longhorn_backups  # noqa: E402

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


def weekly_backup_shard(pvc: str, weekly_volumes: dict) -> int:
    """The weekday shard of `pvc` in `weekly_volumes`, the map `longhorn_weekly_claims` returns.

    Raises `ValueError` when `pvc` is not in the map, as the list lookup this replaced did.
    """
    if pvc not in weekly_volumes:
        raise ValueError(f"weekly_backup_shard: {pvc!r} is not a weekly-tier volume")
    return weekly_volumes[pvc]


def _entries(containers_list):
    return [e for e in containers_list or [] if isinstance(e, dict)]


def longhorn_weekly_claims(containers_list, namespace: str) -> dict:
    """`{namespace/claim: shard}` for every entry's `weekly_backup_claims`, in list order.

    DECIDED: the shard is declared per claim, never derived from a position. It was the
    claim's index mod 7 in a hand list, so one deletion moved every volume below it to another
    weekday and could stack two heavy volumes on one B2 cap-day (#4207).

    `namespace` is the default for an entry that names none; pass `k8s_namespace`. Raises
    `ValueError` on a shard outside 0 to 6 or a claim two entries declare.
    """
    found: dict[str, int] = {}
    for entry in _entries(containers_list):
        for claim, shard in (entry.get("weekly_backup_claims") or {}).items():
            pvc = f"{entry.get('namespace') or namespace}/{claim}"
            # YAML's `true` is a bool, and a bool is an int. Not `type(shard) is int`: Ansible
            # passes its own tagged int subclass.
            if (
                isinstance(shard, bool)
                or not isinstance(shard, int)
                or shard not in _SHARDS
            ):
                raise ValueError(
                    f"longhorn_weekly_claims: {pvc} declares shard {shard!r}, "
                    f"not an integer 0-{len(_SHARDS) - 1}"
                )
            if pvc in found:
                raise ValueError(f"longhorn_weekly_claims: {pvc} is declared twice")
            found[pvc] = int(shard)
    return found


def longhorn_nobackup_claims(containers_list, namespace: str) -> list:
    """`namespace/claim` for every entry's `no_backup_claims`, in list order.

    `namespace` is the default for an entry that names none; pass `k8s_namespace`.
    """
    return [
        f"{entry.get('namespace') or namespace}/{claim}"
        for entry in _entries(containers_list)
        for claim in entry.get("no_backup_claims") or []
    ]


# The bare names `longhorn_backup_name` returns, by key.
NAMES = {
    "label_prefix": longhorn_backups.GROUP_LABEL_PREFIX,
    "default_group": longhorn_backups.DEFAULT_GROUP,
    "no_backup_group": longhorn_backups.NO_BACKUP_GROUP,
    "weekly_legacy_group": longhorn_backups.WEEKLY_LEGACY_GROUP,
    "b2_target": longhorn_backups.B2_TARGET,
    "r2_target": longhorn_backups.R2_TARGET,
}


def longhorn_backup_name(key: str) -> str:
    """The name `key` stands for in `NAMES`, refusing a key it does not list."""
    if key not in NAMES:
        raise ValueError(
            f"longhorn_backup_name: {key!r} is not a Longhorn backup name; "
            f"known: {', '.join(sorted(NAMES))}"
        )
    return NAMES[key]


class FilterModule:
    """Registers the Longhorn backup group filters."""

    def filters(self):
        return {
            "backup_group_label": backup_group_label,
            "weekly_backup_group": weekly_backup_group,
            "weekly_backup_shard": weekly_backup_shard,
            "longhorn_weekly_claims": longhorn_weekly_claims,
            "longhorn_nobackup_claims": longhorn_nobackup_claims,
            "longhorn_backup_name": longhorn_backup_name,
        }
