#!/usr/bin/env python3
"""Run the stop conditions of `docs/longhorn-disaster-recovery.md` in order, exit code naming the first failure.

The recovery procedure's one ordering rule with teeth is step 4: restore the volumes BEFORE
any `deploy.yml`, because a deploy first provisions fresh, empty PVCs under the very names the
backups would have restored into. Steps 2 and 3 are what make step 4 possible at all. The
runbook carried these as prose and a hand-run `kubectl get backuptarget`; here each is a
verdict over what the rebuilt cluster answered, the runner stops at the first failure, and the
exit code is the gate number (#2216, the shape `k3s_upgrade_gates.py` set in #2162). Run it
on the rebuilt cluster after bring-up and before the first restore.

The gates, in the order the runbook gives them:

  1. Both backup targets are armed and available. The four R2 volumes restore only from the
     `r2` target, so a B2-only bring-up leaves them with nothing to restore from; and an armed
     target that is not `available` cannot list what it holds.
  2. The backupstore has synced. The poll interval is 0, so nothing syncs on its own: until
     Backup → Sync is forced in the UI, no `BackupVolume` exists and there is nothing to
     restore. Every required target must show at least one.
  3. No backed-up volume has already been provisioned empty. For each `BackupVolume`, the
     PVC it was taken from (its `KubernetesStatus` label) must not be bound to a live volume
     that was not created from a backup. A live cluster fails this gate on every volume —
     which is correct, because this runbook is for a cluster that has lost them.

Every cluster read goes through `lib.kubectl` with the cluster named `prod` (#1663). The B2
transaction cap is not scriptable from here: a cap denial surfaces as `cannot find volume.cfg
in backupstore`, and the runbook's step 3 says what to do when you see it.

Exit codes:
  0      every gate passed
  1..3   the first gate that failed, by its number above
  69     the cluster could not be asked (no kubectl, no readable kubeconfig, wrong cluster,
         or a list that returned nothing parseable)

Usage:
    uv run python scripts/deploy_tools/longhorn_dr_gates.py
"""

import json
import sys
from pathlib import Path as _Path

# Reach `lib`: a directly-invoked script gets only its own directory on sys.path, and
# pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from deploy_tools import runbook_gates
from deploy_tools.runbook_gates import (
    LONGHORN_NS,
    TARGETS_ARGS,
    VOLUMES_ARGS,
    Gate,
    cluster_doc,
    items,
    name_of,
    unreachable_targets,
)
from lib.kubectl import DEFAULT_TOOLS, Tools

CLUSTER = "prod"
RUNBOOK = "docs/longhorn-disaster-recovery.md"

# The two targets the k3s role renders: B2 (`default`) and R2. Both must hold what they hold
# for a full recovery, so both are required here where the upgrade runbook needs only `default`.
REQUIRED_TARGETS = ("default", "r2")

BACKUP_VOLUMES_ARGS = (
    "-n",
    LONGHORN_NS,
    "get",
    "backupvolumes.longhorn.io",
    "-o",
    "json",
)


# ── the gates, as pure verdicts over what the cluster answered ──────────────────────────────


def _target_of(backup_volume: dict) -> str:
    return str((backup_volume.get("spec") or {}).get("backupTargetName") or "")


def unsynced_targets(backup_volumes_doc, required=REQUIRED_TARGETS) -> list[str]:
    """Required targets with no `BackupVolume` — the sync has not happened for them."""
    if backup_volumes_doc is None:
        return ["<could not list backupvolumes.longhorn.io>"]
    synced = {_target_of(item) for item in items(backup_volumes_doc)}
    return [
        f"{name} (no BackupVolume yet — force Backup → Sync in the Longhorn UI)"
        for name in required
        if name not in synced
    ]


def pvc_of_backup(backup_volume: dict) -> tuple[str, str] | None:
    """`(namespace, pvcName)` from the `KubernetesStatus` label Longhorn writes, or None.

    The label is a JSON document in a string. A backup volume without one (a volume that was
    never bound to a PVC) has no name to collide with.
    """
    raw = ((backup_volume.get("status") or {}).get("labels") or {}).get(
        "KubernetesStatus"
    )
    if not raw:
        return None
    try:
        k8s = json.loads(raw)
    except TypeError, ValueError:
        return None
    namespace, pvc = k8s.get("namespace"), k8s.get("pvcName")
    if not namespace or not pvc:
        return None
    return str(namespace), str(pvc)


def provisioned_empty(backup_volumes_doc, volumes_doc) -> list[str]:
    """Live volumes bound to a backed-up PVC's name that were not created from a backup."""
    if backup_volumes_doc is None:
        return ["<could not list backupvolumes.longhorn.io>"]
    if volumes_doc is None:
        return ["<could not list volumes.longhorn.io>"]
    backed_up: dict[tuple[str, str], str] = {}
    for item in items(backup_volumes_doc):
        pvc = pvc_of_backup(item)
        if pvc is not None:
            backed_up.setdefault(pvc, name_of(item))
    found = []
    for item in items(volumes_doc):
        k8s = (item.get("status") or {}).get("kubernetesStatus") or {}
        pvc = (str(k8s.get("namespace") or ""), str(k8s.get("pvcName") or ""))
        if pvc not in backed_up:
            continue
        if (item.get("spec") or {}).get("fromBackup"):
            continue
        found.append(
            f"{pvc[0]}/{pvc[1]} is bound to {name_of(item)}, provisioned empty — "
            f"a deploy ran before the restore (backup volume {backed_up[pvc]})"
        )
    return found


# ── the runner ──────────────────────────────────────────────────────────────────────────────


def _doc(tools: Tools, args: tuple[str, ...]):
    return cluster_doc(CLUSTER, tools, args)


def _gate_targets(tools: Tools) -> list[str]:
    return unreachable_targets(_doc(tools, TARGETS_ARGS), REQUIRED_TARGETS)


def _gate_synced(tools: Tools) -> list[str]:
    return unsynced_targets(_doc(tools, BACKUP_VOLUMES_ARGS))


def _gate_not_provisioned(tools: Tools) -> list[str]:
    return provisioned_empty(
        _doc(tools, BACKUP_VOLUMES_ARGS), _doc(tools, VOLUMES_ARGS)
    )


# Order is the runbook's, and the exit code is the position. Append; never reorder.
GATES = (
    Gate(1, "both backup targets armed and available", _gate_targets),
    Gate(2, "the backupstore has synced on every target", _gate_synced),
    Gate(3, "no backed-up volume provisioned empty", _gate_not_provisioned),
)


def run_gates(tools: Tools = DEFAULT_TOOLS, out=sys.stdout) -> int:
    """Run every gate in order, print one line per gate, and return the exit code."""
    return runbook_gates.run_gates(GATES, RUNBOOK, tools, out=out)


def main(argv: list[str] | None = None) -> int:
    return runbook_gates.cli(__doc__, argv, run_gates)


if __name__ == "__main__":
    sys.exit(main())
