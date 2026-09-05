"""The live cluster objects the B2 reports read: Volume, Backup, PV and BackupTarget.

Split out of probe_lib/longhorn.py, which had grown to 630 lines. Every function here is one
read-only `kubectl get -o json` plus the projection the reports want from it — the shard a
volume belongs to, how many of its backups its current job owns, its PVC name, and which URL
a BackupTarget points at.

Each takes an injectable `_run` so the projections are testable without a cluster.
longhorn.py keeps the subcommands that drive these, and b2_ledger.py reads `pvc_names` and
`backup_target_url` through that facade.
"""

import json
import subprocess


def volume_shard_labels(_run=None):
    """{longhorn volume: recurring-job group} from the live cluster."""
    run = _run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    out = run(
        [
            "kubectl",
            "-n",
            "longhorn-system",
            "get",
            "volumes.longhorn.io",
            "-o",
            "json",
        ]
    )
    if out.returncode != 0:
        raise SystemExit("kubectl failed: " + out.stderr.strip()[:300])
    shards = {}
    for item in json.loads(out.stdout).get("items", []):
        for key in item.get("metadata", {}).get("labels", {}):
            # Exact group comparison, not a `<group>/` prefix test: see the same rewrite in
            # scripts/infra_map/live.py for why the prefix form is read as URL sanitization.
            group, sep, name = key.partition("/")
            if sep and group == "recurring-job-group.longhorn.io":
                shards[item["metadata"]["name"]] = name
    return shards


def volume_owned_backup_counts(_run=None):
    """{longhorn volume: how many of its backups its CURRENT recurring job owns}.

    Longhorn stamps the producing job onto `.status.labels.RecurringJob`. That is a Longhorn
    STATUS field, not a Kubernetes label, so `kubectl -l` cannot select on it.
    """
    run = _run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    out = run(
        ["kubectl", "-n", "longhorn-system", "get", "backups.longhorn.io", "-o", "json"]
    )
    if out.returncode != 0:
        raise SystemExit("kubectl failed: " + out.stderr.strip()[:300])
    owners = {}
    for item in json.loads(out.stdout).get("items", []):
        status = item.get("status", {})
        vol = status.get("volumeName")
        job = (status.get("labels") or {}).get("RecurringJob")
        if vol and job:
            owners.setdefault(vol, {})[job] = owners.setdefault(vol, {}).get(job, 0) + 1
    return owners


def pvc_names(_run=None):
    """{longhorn volume: PVC name}, so the report reads in service terms."""
    run = _run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    out = run(["kubectl", "get", "pv", "-o", "json"])
    if out.returncode != 0:
        return {}
    return {
        pv["metadata"]["name"]: pv["spec"].get("claimRef", {}).get("name", "")
        for pv in json.loads(out.stdout).get("items", [])
        if pv.get("spec", {}).get("claimRef")
    }


# --- which BackupTarget is B2 -----------------------------------------------------------------

# The cluster holds two BackupTargets and only one of them is B2: `default` carries the B2 URL
# (`longhorn-backup.yml` patches it with `kopia_b2_bucket`), `r2` carries Cloudflare's
# (`longhorn-backuptarget-r2.yaml.j2`). R2's caps are monthly and vast, so charging an R2
# deletion against B2's 2,500/day Class C cap would inflate the ledger against a cap that does
# not apply to it. The deletion log line names the target URL, not the target NAME, so the
# mapping has to come from the CRs — inferring B2 from the `us-east-005` region component would
# be exactly the guess `docs/adr/0014` and the tiering memory warn against.
B2_BACKUP_TARGET_NAME = "default"


def backup_target_url(name=B2_BACKUP_TARGET_NAME, _run=None):
    """The `spec.backupTargetURL` of one BackupTarget, or "" when it is absent or disarmed.

    Empty is a real state, not an error: `k3s_longhorn_backup_armed: false` enforces a BLANK
    URL as the containment lever for a cap spiral. A caller that gets "" must decline to
    classify rather than fall back to matching everything.
    """
    run = _run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    out = run(
        [
            "kubectl",
            "-n",
            "longhorn-system",
            "get",
            "backuptargets.longhorn.io",
            name,
            "-o",
            "json",
        ]
    )
    if out.returncode != 0:
        return ""
    try:
        return json.loads(out.stdout).get("spec", {}).get("backupTargetURL", "") or ""
    except json.JSONDecodeError:
        return ""
