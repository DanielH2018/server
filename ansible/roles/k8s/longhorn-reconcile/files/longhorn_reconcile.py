#!/usr/bin/env python3
"""The `longhorn-reconcile` CronJob: hold each Longhorn volume in the recurring-job group and backup target its tier names.

Longhorn reads a volume's backup schedule off its own Volume CR labels
(`recurring-job-group.longhorn.io/<group>`), and a PVC's StorageClass decides the group only at
provision time. A volume that must LEAVE the daily set after it is bound cannot be moved by
editing either, so this job reconciles the label. It replaces the label, weekly-shard and
legacy-label tasks that `setup/k3s` ran from `tasks/longhorn.yml` and
`tasks/longhorn-weekly-shard.yml` (H1 plan, H1.6). From H1.7a it also holds each volume's
`spec.backupTargetName`, which the two routing tasks of `tasks/longhorn-backup.yml` held.

What one run does, in this order, for each volume:

1. A volume in the daily group (`default`) whose claim is on the no-backup list moves to
   `no-backup`.
2. A volume in `no-backup` whose claim is `longhorn`-class and on neither list returns to
   `default`. The class is the intent for volumes that never were on a list: most no-backup
   volumes were excluded by `longhorn-nobackup` at creation, and sweeping them into the daily
   set would be the transaction blowup this job exists to prevent.
3. A volume in `default` whose claim is on the weekly list moves to its weekday shard,
   `weekly-backup-d<N>`, the shard its `containers_list` entry declares (0=Sunday).
4. A volume carrying the retired flat `weekly-backup` label moves to its shard if its claim is
   on the weekly list, and returns to `default` if not. No RecurringJob selects the flat label,
   so a volume stuck with it is silently unprotected, and this stays as the guard against a
   hand-applied legacy label.
5. A volume in a weekly shard group whose claim is off the weekly list returns to `default`.
   One whose claim declares another shard moves to that shard.

The steps run against each volume's own labels, in that order. Then one more step sets the
backup target:

6. A volume whose claim is on the `r2` list and whose `spec.backupTargetName` is not `r2` moves
   to `r2`. A volume on `r2` whose claim is on no `r2` list returns to `default`, the B2 target.
   A volume whose target is blank, or any other name, and whose claim is not listed is left
   alone, as the Ansible routing tasks left it.

Then one more step sets the trim label (H2.2a, the H2 plan's *Native trim*):

7. A volume that is `attached` and has a Running ReplicaSet or StatefulSet workload gets the
   per-job label `recurring-job.longhorn.io/trim: enabled`, which puts it in Longhorn's `trim`
   RecurringJob. It loses the label when either condition stops holding. A volume that gains the
   label and sits in the daily tier also gets `recurring-job-group.longhorn.io/default`
   explicitly, because a volume with "a job of its own" no longer gets `default` from Longhorn.
   The step writes no other group key, and never a `recurring-job-group.longhorn.io/trim` one.
   `tiers.json`'s optional `trim_claims` list bounds the step: only a listed claim keeps the
   label, an empty list admits none, and an absent key admits every volume the rule admits.
   Every run also prints `trim would-admit: <claims>`, the claims the UNBOUNDED rule admits.

The run writes ONE merge patch per changed volume: the net of the steps, labels and target
together. A converged cluster writes nothing.

It does not touch the CSI sidecar Deployments. Their memory request is the `longhorn` role's
`system-managed-csi-components-resource-limits` Setting, which longhorn-manager enforces and
which clears any request another writer sets (#5958).

Inputs: `/config/tiers.json` (the ConfigMap's `{"r2": [...], "weekly": {claim: shard}, "nobackup":
[...]}` and an optional `"trim_claims": [...]`, claims as `namespace/pvcName`) and the environment: `RECONCILE_NAMESPACE`, and
optionally `RECONCILE_TIERS_FILE`.

It talks to the API server with the pod's ServiceAccount token, and the API server's CA reaches
`urlopen` through `SSL_CERT_FILE`, which the CronJob sets. Every connection goes through
`flux_job_connect`.

`--report-only` makes the reads and prints what a run WOULD change, with no write. It is the way
to check a change against a live cluster: run it with a read-only credential and expect no
`would patch` line on a converged cluster.

Exit 0 when every volume holds its desired state. Exit 1 when a read fails (before any write), a
claim is on two tier lists, or a write fails. `-h` and `--help` print this text.

Usage:

    python3 /config/longhorn_reconcile.py [--report-only]
"""

import argparse
import json
import os
import sys
import urllib.error
from collections.abc import Callable, Mapping
from pathlib import Path

from flux_job_connect import Request, open_url

API = "https://kubernetes.default.svc"
TOKEN_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/token"
TIERS_FILE = "/config/tiers.json"
# The seconds each API call may take. `open_url` retries a refused connect on top.
CALL_TIMEOUT = 10

# A Volume CR's recurring-job group labels. The names are a contract with the cluster, held
# equal to `setup/k3s`'s `longhorn_lib/longhorn_backups.py` by tests/test_longhorn_reconcile.py.
LABEL_PREFIX = "recurring-job-group.longhorn.io/"
DEFAULT = "default"
NO_BACKUP = "no-backup"
LEGACY_WEEKLY = "weekly-backup"
SHARDS = range(7)
ENABLED = "enabled"

# The per-job label that puts a volume in the `trim` RecurringJob. It is a per-JOB label, never a
# `recurring-job-group.longhorn.io/trim` group label: four readers treat every key under
# LABEL_PREFIX as a backup tier. tests/test_longhorn_reconcile_trim.py types the literal out.
TRIM_LABEL = "recurring-job.longhorn.io/trim"
# The workload kinds whose volume the trim may join: a CronJob's Job leaves its volume detached
# between runs, and the trim must not attach it.
TRIM_WORKLOAD_TYPES = ("ReplicaSet", "StatefulSet")

# The BackupTarget names a Volume CR's `spec.backupTargetName` takes: `default` is the B2 target
# and `r2` the Cloudflare one. They are a contract with the cluster, held equal to
# `setup/k3s`'s `longhorn_lib/longhorn_backups.py` by tests/test_longhorn_reconcile.py.
B2_TARGET = "default"
R2_TARGET = "r2"

MERGE_PATCH = "application/merge-patch+json"


class Failed(Exception):
    """A read or a write failed, or the tier input is unusable; the message says which."""


def shard_group(shard: int) -> str:
    """The group name of weekly shard `shard`."""
    return f"weekly-backup-d{shard}"


def label(group: str) -> str:
    """The label key that puts a volume in `group`."""
    return LABEL_PREFIX + group


class Kube:
    """The API server, as one JSON call per method.

    Attributes:
        base: The API server's URL.
        token: The ServiceAccount token sent as a bearer credential.
        opener: Opens a `Request`; `open_url` unless a test passes its own.
    """

    def __init__(self, base: str, token: str, opener: Callable = open_url):
        self.base = base
        self.token = token
        self.opener = opener

    def call(
        self,
        method: str,
        path: str,
        body: Mapping | None = None,
        content_type: str = MERGE_PATCH,
    ) -> tuple[int, dict]:
        """`method` on `path`; the HTTP status and the parsed body, an error status included.

        A body goes as a patch of `content_type`. The API server answers a PATCH sent as plain
        `application/json` with 415.
        """
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = content_type
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.opener(request, timeout=CALL_TIMEOUT) as response:
                return response.status, _parse(response.read())
        except urllib.error.HTTPError as err:
            with err:
                return err.code, _parse(err.read())


def _parse(raw: bytes) -> dict:
    try:
        value = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _ok(status: int, body: dict, what: str) -> dict:
    if status != 200:
        raise Failed(f"{what} answered HTTP {status}: {body.get('message', '')}")
    return body


def load_tiers(path: str | Path) -> dict:
    """The tier document the ConfigMap carries, validated.

    Raises:
        Failed: The file is unreadable or is not `{"r2": list, "weekly": {claim: 0-6}, "nobackup": list}`
            with an optional `"trim_claims": list`, or one claim sits on both the weekly and the
            no-backup list.
    """
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, ValueError) as err:
        raise Failed(f"tier file {path}: {err}") from err
    if not isinstance(doc, dict):
        raise Failed(f"tier file {path} is not a JSON object")
    r2, weekly, nobackup = doc.get("r2"), doc.get("weekly"), doc.get("nobackup")
    if not isinstance(r2, list) or not isinstance(nobackup, list):
        raise Failed("tier file: `r2` and `nobackup` must be lists")
    if not isinstance(weekly, dict):
        raise Failed("tier file: `weekly` must map a claim to its shard")
    for claim, shard in weekly.items():
        if isinstance(shard, bool) or not isinstance(shard, int) or shard not in SHARDS:
            raise Failed(f"tier file: {claim} declares shard {shard!r}, not 0-6")
    both = sorted(set(weekly) & set(nobackup))
    if both:
        # DECIDED: refuse, where the Ansible tasks did not. A claim on both lists has no single
        # desired group; the tasks gave such a volume both labels, which nothing intended, and
        # picking one here would hide a wrong `containers_list` entry. The run stops before it
        # writes anything, so the next hourly run retries once the entry is fixed.
        raise Failed(f"claim(s) on both the weekly and the no-backup list: {both}")
    trim_claims = doc.get("trim_claims")
    if trim_claims is not None and not (
        isinstance(trim_claims, list) and all(isinstance(c, str) for c in trim_claims)
    ):
        raise Failed("tier file: `trim_claims` must be a list of claims")
    return {
        "r2": set(r2),
        "weekly": weekly,
        "nobackup": set(nobackup),
        # None means no bound. An empty set means no volume. They are never the same.
        "trim_claims": None if trim_claims is None else set(trim_claims),
    }


def claim_of(volume: dict) -> str:
    """`namespace/pvcName` of the PVC a Volume CR serves, or `/` when it serves none."""
    kubernetes = (volume.get("status") or {}).get("kubernetesStatus") or {}
    return f"{kubernetes.get('namespace', '')}/{kubernetes.get('pvcName', '')}"


def desired_labels(
    labels: Mapping[str, str],
    claim: str,
    tiers: Mapping,
    class_claims: set[str],
    trim: bool = False,
) -> dict[str, str]:
    """`labels` after the reconcile steps (module docstring) for the volume serving `claim`.

    `trim` says whether the volume belongs in the trim job. Only the group labels and the trim
    label move. Every other label is returned as it was.
    """
    weekly, nobackup = tiers["weekly"], tiers["nobackup"]
    state = dict(labels)

    def has(group: str) -> bool:
        return state.get(label(group)) == ENABLED

    def move(to: str, leaving: str) -> None:
        state[label(to)] = ENABLED
        state.pop(label(leaving), None)

    in_default = has(DEFAULT)
    # 1. The daily group, minus the no-backup list.
    if in_default and claim in nobackup:
        move(NO_BACKUP, DEFAULT)
    # 2. Back to daily: the class asks for backups and no list overrides it.
    if (
        has(NO_BACKUP)
        and claim in class_claims
        and claim not in nobackup
        and claim not in weekly
    ):
        move(DEFAULT, NO_BACKUP)
    # 3. The daily group, minus the weekly list. `in_default` is the state before step 1,
    #    as the Ansible query was.
    if in_default and claim in weekly:
        move(shard_group(weekly[claim]), DEFAULT)
    # 4. The retired flat weekly label.
    if has(LEGACY_WEEKLY):
        move(shard_group(weekly[claim]) if claim in weekly else DEFAULT, LEGACY_WEEKLY)
    # 5. Each shard's membership, in shard order.
    for shard in SHARDS:
        if not has(shard_group(shard)):
            continue
        if claim not in weekly:
            move(DEFAULT, shard_group(shard))
        elif weekly[claim] != shard:
            move(shard_group(weekly[claim]), shard_group(shard))
    # 7. The trim label. A volume that gains it and sits in the daily tier is also put in
    #    `default` explicitly, so it keeps exactly one group label.
    if not trim:
        state.pop(TRIM_LABEL, None)
        return state
    state[TRIM_LABEL] = ENABLED
    other_groups = [NO_BACKUP, LEGACY_WEEKLY, *(shard_group(s) for s in SHARDS)]
    if (
        claim not in weekly
        and claim not in nobackup
        and not any(has(group) for group in other_groups)
    ):
        state[label(DEFAULT)] = ENABLED
    return state


def trim_rule(volume: dict) -> bool:
    """The unbounded rule: the volume is attached and has a Running ReplicaSet or StatefulSet.

    A CronJob's Job, a Failed or Succeeded pod and a scaled-to-zero workload do not qualify, so
    the trim never attaches a volume that Longhorn keeps detached.
    """
    status = volume.get("status") or {}
    if status.get("state") != "attached":
        return False
    return any(
        workload.get("podStatus") == "Running"
        and workload.get("workloadType") in TRIM_WORKLOAD_TYPES
        for workload in (status.get("kubernetesStatus") or {}).get("workloadsStatus")
        or []
    )


def wants_trim(volume: dict, claim: str, tiers: Mapping) -> bool:
    """Whether the volume joins the trim job: the unbounded rule, then the canary bound."""
    bound = tiers["trim_claims"]
    return trim_rule(volume) and (bound is None or claim in bound)


def target_of(volume: dict) -> str:
    """The `spec.backupTargetName` a Volume CR holds, or an empty string when it names none."""
    return str((volume.get("spec") or {}).get("backupTargetName") or "")


def desired_target(current: str, claim: str, r2: set[str]) -> str | None:
    """The target to patch a volume to, or None when it is already where its claim belongs.

    A claim on the `r2` list goes to `r2`. A volume on `r2` whose claim is on no list returns
    to `default`. Any other volume keeps what it has, a blank target included: the Ansible
    routing tasks moved only these two cases, and sweeping a blank target onto `default` would
    patch every volume the first run meets.
    """
    if claim in r2:
        return None if current == R2_TARGET else R2_TARGET
    return B2_TARGET if current == R2_TARGET else None


def group_keys() -> set[str]:
    """Every group label key this job writes or removes."""
    groups = [DEFAULT, NO_BACKUP, LEGACY_WEEKLY, *(shard_group(s) for s in SHARDS)]
    return {label(group) for group in groups}


def label_keys() -> set[str]:
    """Every label key this job writes or removes: the group keys and the trim label."""
    return group_keys() | {TRIM_LABEL}


def plan(volumes: list[dict], class_claims: set[str], tiers: Mapping) -> list[dict]:
    """One merge patch per volume whose group labels or backup target must change, by name.

    Each entry is `{"name", "claim", "labels", "target"}`. `labels` maps a group label key to
    its new value, or to None to remove it, and is empty when only the target moves. `target`
    is the new `spec.backupTargetName`, or None when the volume keeps its own.
    """
    keys = label_keys()
    out = []
    for volume in sorted(volumes, key=lambda v: v["metadata"]["name"]):
        before = volume["metadata"].get("labels") or {}
        claim = claim_of(volume)
        after = desired_labels(
            before, claim, tiers, class_claims, wants_trim(volume, claim, tiers)
        )
        delta = {}
        for key in sorted(keys):
            if after.get(key) != before.get(key):
                delta[key] = after.get(key)
        target = desired_target(target_of(volume), claim, tiers["r2"])
        if delta or target:
            out.append(
                {
                    "name": volume["metadata"]["name"],
                    "claim": claim,
                    "labels": delta,
                    "target": target,
                }
            )
    return out


def describe(change: dict) -> str:
    """One report line for a planned volume patch."""
    parts = [
        f"{key.removeprefix(LABEL_PREFIX)}{'-' if value is None else '=' + value}"
        for key, value in change["labels"].items()
    ]
    if change["target"]:
        parts.append(f"backupTargetName={change['target']}")
    return f"volume {change['name']} ({change['claim']}): {' '.join(parts)}"


def would_admit(volumes: list[dict]) -> str:
    """The `trim would-admit:` line: the claims the UNBOUNDED rule admits, sorted.

    The canary bound keeps the trim label off every other volume, so the bound cannot test the
    rule. This line shows what H2.2b would admit, and it prints whatever the bound is.
    """
    claims = sorted({claim_of(v) for v in volumes if trim_rule(v)})
    return "trim would-admit: " + (", ".join(claims) if claims else "none")


def class_claims_of(pvcs: list[dict]) -> set[str]:
    """`namespace/name` of every PVC whose StorageClass is `longhorn`, the one that asks for backups."""
    return {
        f"{(pvc.get('metadata') or {}).get('namespace', '')}/{(pvc.get('metadata') or {}).get('name', '')}"
        for pvc in pvcs
        if (pvc.get("spec") or {}).get("storageClassName") == "longhorn"
    }


def reconcile_labels(
    kube: Kube, namespace: str, tiers: Mapping, report_only: bool
) -> list[str]:
    """Plan the volume changes from two list reads, then write them. One report line per change.

    Both reads happen before the first write, so a failed read changes nothing.
    """
    volumes_path = f"/apis/longhorn.io/v1beta2/namespaces/{namespace}/volumes"
    volumes = _ok(*kube.call("GET", volumes_path), "listing volumes").get("items") or []
    pvcs = (
        _ok(*kube.call("GET", "/api/v1/persistentvolumeclaims"), "listing PVCs").get(
            "items"
        )
        or []
    )
    changes = plan(volumes, class_claims_of(pvcs), tiers)
    lines = []
    for change in changes:
        if not report_only:
            body: dict = {}
            if change["labels"]:
                body["metadata"] = {"labels": change["labels"]}
            if change["target"]:
                body["spec"] = {"backupTargetName": change["target"]}
            status, answer = kube.call(
                "PATCH", f"{volumes_path}/{change['name']}", body, MERGE_PATCH
            )
            _ok(status, answer, f"patching volume {change['name']}")
        lines.append(("would patch " if report_only else "patched ") + describe(change))
    lines.append(would_admit(volumes))
    lines.append(
        f"{len(volumes)} volume(s) read, {len(changes)} volume change(s)"
        f"{' planned' if report_only else ''}"
    )
    return lines


def run(kube: Kube, env: Mapping[str, str], report_only: bool) -> list[str]:
    """The whole reconcile. Returns the report lines.

    Raises:
        Failed: A read or a write failed, or the tier input is unusable.
    """
    tiers = load_tiers(env.get("RECONCILE_TIERS_FILE", TIERS_FILE))
    return reconcile_labels(kube, env["RECONCILE_NAMESPACE"], tiers, report_only)


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    kube: Kube | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="read the cluster and print what a run would change, with no write",
    )
    args = parser.parse_args(argv)
    if kube is None:
        kube = Kube(API, Path(TOKEN_FILE).read_text().strip())
    try:
        lines = run(kube, os.environ if env is None else env, args.report_only)
    except Failed as err:
        print(f"longhorn-reconcile: {err}", file=sys.stderr)
        return 1
    for line in lines:
        print(f"longhorn-reconcile: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
