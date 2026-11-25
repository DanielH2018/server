"""The live cluster objects the B2 reports read: Volume, Backup, PV and BackupTarget.

Split out of probe_lib/longhorn.py, which had grown to 630 lines. Every function here is one
read-only `kubectl get -o json` plus the projection the reports want from it — the shard a
volume belongs to, how many of its backups its current job owns, its PVC name, and which URL
a BackupTarget points at.

Each names the cluster it reads (production by default) and takes an injectable `_run` —
`lib.kubectl.kubectl` minus its cluster argument — so the projections are testable without one.
longhorn.py keeps the subcommands that drive these, and b2_ledger.py reads `pvc_names`,
`volume_backup_targets` and `backup_target_url` through that facade.
"""

import json

from diagnostics.probe_lib.core import Unreadable
from lib.k8s_objects import BackupTarget
from lib.kubectl import DEFAULT_CLUSTER, kubectl

# The Backup CR reader and the backup group and target names, shared with the backup-health cron.
from lib.longhorn_lib import longhorn_backups


def volume_shard_labels(cluster=DEFAULT_CLUSTER, _run=None):
    """{longhorn volume: recurring-job group} from the live cluster."""
    run = _run or (lambda *args: kubectl(cluster, *args))
    out = run(
        "-n",
        "longhorn-system",
        "get",
        "volumes.longhorn.io",
        "-o",
        "json",
    )
    if out.returncode != 0:
        raise Unreadable("kubectl failed: " + out.stderr.strip()[:300])
    shards = {}
    for item in json.loads(out.stdout).get("items", []):
        for key in item.get("metadata", {}).get("labels", {}):
            # Exact group comparison, not a `<group>/` prefix test, which reads as URL sanitization
            # (see infra_map/live.py).
            group, sep, name = key.partition("/")
            if sep and group + sep == longhorn_backups.GROUP_LABEL_PREFIX:
                shards[item["metadata"]["name"]] = name
    return shards


def volume_owned_backup_counts(cluster=DEFAULT_CLUSTER, _run=None):
    """{longhorn volume: how many of its backups its CURRENT recurring job owns}.

    Longhorn stamps the producing job onto `.status.labels.RecurringJob`. That is a Longhorn
    STATUS field, not a Kubernetes label, so `kubectl -l` cannot select on it.
    """
    run = _run or (lambda *args: kubectl(cluster, *args))
    out = run("-n", "longhorn-system", *longhorn_backups.LIST_ARGS)
    if out.returncode != 0:
        raise Unreadable("kubectl failed: " + out.stderr.strip()[:300])
    owners = {}
    items = json.loads(out.stdout).get("items", [])
    for backup in longhorn_backups.from_items(items):
        if backup.volume and backup.job:
            jobs = owners.setdefault(backup.volume, {})
            jobs[backup.job] = jobs.get(backup.job, 0) + 1
    return owners


def pvc_names(cluster=DEFAULT_CLUSTER, _run=None):
    """{longhorn volume: PVC name}, so the report reads in service terms."""
    run = _run or (lambda *args: kubectl(cluster, *args))
    out = run("get", "pv", "-o", "json")
    if out.returncode != 0:
        return {}
    return {
        pv["metadata"]["name"]: pv["spec"].get("claimRef", {}).get("name", "")
        for pv in json.loads(out.stdout).get("items", [])
        if pv.get("spec", {}).get("claimRef")
    }


# --- which BackupTarget is B2 -----------------------------------------------------------------

# Only `default` is B2 (`r2` is Cloudflare, with vast monthly caps). The deletion line names the
# URL, not the target, so map via the CRs and never infer from the region.
B2_BACKUP_TARGET_NAME = longhorn_backups.B2_TARGET


def volume_backup_targets(cluster=DEFAULT_CLUSTER, _run=None):
    """{longhorn volume: spec.backupTargetName}, so a report can tell B2 rows from R2 ones.

    A volume with no target set is returned as "" rather than omitted: absent from the map and
    present-but-blank are different states, and a caller that has to decide which store a
    backup went to needs to tell them apart.

    `pvc_names` cannot answer this. It reads PVs, and `backupTargetName` lives on the Longhorn
    Volume CR — the same object `volume_shard_labels` reads. The two key spaces coincide because
    a Longhorn volume is named after its PV (`pvc-<uuid>`).
    """
    run = _run or (lambda *args: kubectl(cluster, *args))
    out = run("-n", "longhorn-system", "get", "volumes.longhorn.io", "-o", "json")
    if out.returncode != 0:
        return {}
    try:
        items = json.loads(out.stdout).get("items", [])
    except json.JSONDecodeError:
        return {}
    return {
        (item.get("metadata") or {}).get("name", ""): (item.get("spec") or {}).get(
            "backupTargetName"
        )
        or ""
        for item in items
        if (item.get("metadata") or {}).get("name")
    }


def backup_target_url(name=B2_BACKUP_TARGET_NAME, cluster=DEFAULT_CLUSTER, _run=None):
    """The `spec.backupTargetURL` of one BackupTarget, or "" when it is absent or disarmed.

    Empty is a real state, not an error: `k3s_longhorn_backup_armed: false` enforces a BLANK
    URL as the containment lever for a cap spiral. A caller that gets "" must decline to
    classify rather than fall back to matching everything.
    """
    run = _run or (lambda *args: kubectl(cluster, *args))
    out = run(
        "-n",
        "longhorn-system",
        "get",
        "backuptargets.longhorn.io",
        name,
        "-o",
        "json",
    )
    if out.returncode != 0:
        return ""
    try:
        doc = json.loads(out.stdout)
    except json.JSONDecodeError:
        return ""
    if not isinstance(doc, dict):
        return ""
    return BackupTarget.from_item(doc).url or ""
