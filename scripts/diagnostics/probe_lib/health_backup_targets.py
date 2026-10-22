"""The `probe.py health longhorn-backup` verdict: do the BackupTargets read available?

The `longhorn-backup` role renders no workload, so the rollout gate has nothing to check. Its
post-deploy question is the one the Ansible task `Check whether Longhorn can reach the backup
target` answered: can Longhorn reach the store each armed target points at. Flux applies the
tag, so the question moves here (H1.7b).

A target with a blank `backupTargetURL` is disarmed on purpose (`k3s_longhorn_backup_armed:
false`), so it is named and skipped: a blank target is never `available`, and failing on it
would make a deliberate containment read as a failed deploy. A list with no target named
`default` fails, because an empty or partial list must not pass. A target that carries the
reconcile-disabled annotation fails and is named, because a break-glass is in force and Flux is
not enforcing the target (ADR-0007, second amendment).

Pure functions take the parsed `.items`; `run_backup_target_health` reads them. Every message
here names a RESOLVED object, so none carries a `detach_notify` skip marker (health.py, *what
"not found" is allowed to mean*).
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from diagnostics.probe_lib import core
from lib.exit_codes import PROBE_UNREADABLE

BACKUP_ROLE = "longhorn-backup"
DEFAULT_TARGET = "default"
RECONCILE_ANNOTATION = "kustomize.toolkit.fluxcd.io/reconcile"
RECONCILE_DISABLED = "disabled"
TARGET_ARGS = (
    "-n",
    "longhorn-system",
    "get",
    "backuptargets.longhorn.io",
    "-o",
    "json",
)


def target_verdicts(items: list[dict]) -> tuple[list[str], list[str], list[str]]:
    """`(available, disarmed, problems)` for the BackupTarget `items`.

    A target is armed while its URL holds anything but whitespace. An armed target is available
    only when `status.available` is true. `problems` is empty only when a target named `default`
    is present and every armed target is available and unannotated.
    """
    available: list[str] = []
    disarmed: list[str] = []
    problems: list[str] = []
    names = set()
    for item in items:
        meta, spec, status = (
            item.get("metadata") or {},
            item.get("spec") or {},
            item.get("status") or {},
        )
        name = meta.get("name", "")
        names.add(name)
        if (meta.get("annotations") or {}).get(RECONCILE_ANNOTATION) == (
            RECONCILE_DISABLED
        ):
            problems.append(
                f"backup target {name} carries {RECONCILE_ANNOTATION}={RECONCILE_DISABLED}: "
                "a break-glass is in force and Flux is not enforcing it"
            )
        if not str(spec.get("backupTargetURL") or "").strip():
            disarmed.append(name)
        elif status.get("available") is True:
            available.append(name)
        else:
            conditions = status.get("conditions") or []
            message = (conditions[0] or {}).get("message", "") if conditions else ""
            problems.append(
                f"backup target {name} is armed and not available"
                + (f": {message[:160]}" if message else "")
            )
    if DEFAULT_TARGET not in names:
        problems.append(f"no backup target named {DEFAULT_TARGET} in longhorn-system")
    return sorted(available), sorted(disarmed), problems


def report(cluster, tools, prom, read, with_alert_plane) -> int:
    """The gate for the `longhorn-backup` tag: read the targets, print the verdict, return its code.

    `read` and `with_alert_plane` are `health._read` and `health._with_alert_plane`, handed in
    because this module sits below `health.py` and may not import it.
    """
    try:
        doc = read(cluster, list(TARGET_ARGS), tools)
    except core.Unreadable as exc:
        print(f"{BACKUP_ROLE}: {exc}", file=_sys.stderr)
        return PROBE_UNREADABLE
    text, code = format_backup_target_health((doc or {}).get("items") or [])
    text, code = with_alert_plane(BACKUP_ROLE, text, code, None, prom)
    print(text)
    return code


def format_backup_target_health(items: list[dict]) -> tuple[str, int]:
    """The `longhorn-backup` verdict line or lines, and the exit code (0 clean, 1 flagged)."""
    available, disarmed, problems = target_verdicts(items)
    note = f"; disarmed on purpose: {', '.join(disarmed)}" if disarmed else ""
    if problems:
        lines = [f"{BACKUP_ROLE}: FAILED"] + [f"  {p}" for p in problems]
        return "\n".join(lines) + note, 1
    return (
        f"{BACKUP_ROLE}: available: {', '.join(available) or 'none'}{note}",
        0,
    )
