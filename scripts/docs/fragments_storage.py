"""Doc fragments for the Longhorn, snapshot and deadman pages.

Each entry in `FRAGMENTS` builds one fragment the way `gen_doc_fragments.py` builds its own:
a reader that parses the tree statically, and a pure renderer from what it returned to
markdown. `gen_doc_fragments.FRAGMENTS` merges this table, prepends the provenance header and
writes the file, so a builder returns only `(body, sources)`.

- `snapshot-optin`: the k8s roles that declare `k8s_autodeploy_snapshot_pvcs`, with the
  claims each snapshots before an apply.
- `longhorn-volume-shards`: every volume in a backup tier list, with its target and, for the
  weekly tier, the weekday of its shard. The tier lists resolve through `lib.service_tiers`,
  the same filters the k3s role runs.
- `deadman-graces`: the schedule type and grace of each Healthchecks.io slug, read from
  monitor-bridge's `monitor_bridge_healthchecks_expected`.
"""

import sys as _sys
from collections.abc import Callable
from pathlib import Path as _Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.estate import Estate, role_defaults
from lib.repo_paths import K3S_ROLE, K8S_ROLES
from lib.service_tiers import resolved_tier_lists

SNAPSHOT_KEY = "k8s_autodeploy_snapshot_pvcs"
BRIDGE_ROLE = K8S_ROLES / "monitor-bridge"

# Weekly shard N runs on cron weekday N (`longhorn-recurringjob.yaml.j2`), so 0 is Sunday.
WEEKDAYS = (
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
)


def _code_list(items: list[str]) -> str:
    return ", ".join(f"`{item}`" for item in items)


# --- snapshot-optin -------------------------------------------------------------------------


def snapshot_optin(roles_dir: _Path = K8S_ROLES) -> dict[str, list[str]]:
    """`{role: [pvc, ...]}` for every k8s role whose defaults declare `SNAPSHOT_KEY`.

    The defaults are parsed as YAML, so a role that only mentions the name in a comment does
    not count.
    """
    found = {}
    for role_dir in sorted(roles_dir.iterdir()):
        if not role_dir.is_dir():
            continue
        pvcs = role_defaults(role_dir).get(SNAPSHOT_KEY)
        if pvcs:
            found[role_dir.name] = [str(pvc) for pvc in pvcs]
    return found


def render_snapshot_optin(optin: dict[str, list[str]]) -> str:
    """Renders the roles that opt in to a pre-apply snapshot, one row per role.

    Args:
        optin: `{role: [pvc, ...]}` as `snapshot_optin` returns it.
    """
    lines = [
        f"{len(optin)} k8s roles declare `{SNAPSHOT_KEY}` in their defaults.",
        "",
        "| Role | Claims snapshotted before an apply |",
        "|---|---|",
    ]
    lines += [
        f"| `{role}` | {_code_list(pvcs)} |" for role, pvcs in sorted(optin.items())
    ]
    return "\n".join(lines) + "\n"


def _snapshot_optin() -> tuple[str, list[str]]:
    return render_snapshot_optin(snapshot_optin()), [
        "ansible/roles/k8s/*/defaults/main.yml"
    ]


# --- longhorn-volume-shards -----------------------------------------------------------------


def render_volume_shards(
    r2: list[str], weekly: dict[str, int], nobackup: list[str], namespace: str
) -> str:
    """Renders one row per volume in a backup tier list.

    Args:
        r2: `namespace/claim` of the daily tier, which backs up to R2.
        weekly: `{namespace/claim: shard}` of the weekly tier, which backs up to B2 on the
            weekday of its shard.
        nobackup: `namespace/claim` of the volumes in the `no-backup` group.
        namespace: the namespace stripped from a name that carries it.

    Raises:
        ValueError: when a weekly shard is outside 0 to 6.
    """

    def short(pvc: str) -> str:
        return f"`{pvc.removeprefix(namespace + '/')}`"

    rows = [f"| {short(pvc)} | Daily | R2 | every day |" for pvc in r2]
    for pvc, shard in sorted(weekly.items(), key=lambda item: (item[1], item[0])):
        if shard not in range(len(WEEKDAYS)):
            raise ValueError(f"{pvc}: weekly shard {shard!r} is outside 0-6")
        rows.append(f"| {short(pvc)} | Weekly | B2 | {WEEKDAYS[shard]} |")
    rows += [f"| {short(pvc)} | None | — | — |" for pvc in nobackup]
    lines = [
        "| Volume | Tier | Target | Backup day (UTC) |",
        "|---|---|---|---|",
        *rows,
    ]
    return "\n".join(lines) + "\n"


def _volume_shards() -> tuple[str, list[str]]:
    estate = Estate()
    tiers = resolved_tier_lists(estate.role_vars(K3S_ROLE))
    return render_volume_shards(
        tiers["k3s_longhorn_r2_volumes"],
        tiers["k3s_longhorn_weekly_volumes"],
        tiers["k3s_longhorn_nobackup_volumes"],
        estate.group_vars["k8s_namespace"],
    ), [
        "ansible/roles/setup/k3s/defaults/main.yml",
        "ansible/inventory/host_vars/daniel-box.yml",
    ]


# --- deadman-graces -------------------------------------------------------------------------


def format_grace(seconds: int) -> str:
    """A grace in whole hours when it divides evenly, in minutes otherwise."""
    value, unit = (
        (seconds // 3600, "hour") if seconds % 3600 == 0 else (seconds // 60, "minute")
    )
    return f"{value} {unit}{'' if value == 1 else 's'}"


def render_deadman_graces(expected: list[dict]) -> str:
    """Renders each slug's console schedule type and grace.

    Args:
        expected: the rows of `monitor_bridge_healthchecks_expected`, each carrying `slug`,
            `kind` (`simple` or `cron`) and `grace` in seconds.
    """
    lines = ["| Check slug | Schedule type | Grace |", "|---|---|---|"]
    lines += [
        f"| `{row['slug']}` | {row['kind'].capitalize()} | {format_grace(row['grace'])} |"
        for row in expected
    ]
    return "\n".join(lines) + "\n"


def _deadman_graces() -> tuple[str, list[str]]:
    expected = role_defaults(BRIDGE_ROLE)["monitor_bridge_healthchecks_expected"]
    return render_deadman_graces(expected), [
        "ansible/roles/k8s/monitor-bridge/defaults/main.yml"
    ]


# name -> () -> (body, sources). The name is the file stem a page includes.
FRAGMENTS: dict[str, Callable[[], tuple[str, list[str]]]] = {
    "snapshot-optin": _snapshot_optin,
    "longhorn-volume-shards": _volume_shards,
    "deadman-graces": _deadman_graces,
}
