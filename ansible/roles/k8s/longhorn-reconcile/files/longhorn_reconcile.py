#!/usr/bin/env python3
"""The `longhorn-reconcile` CronJob: hold each Longhorn volume in the recurring-job group its tier names.

Longhorn reads a volume's backup schedule off its own Volume CR labels
(`recurring-job-group.longhorn.io/<group>`), and a PVC's StorageClass decides the group only at
provision time. A volume that must LEAVE the daily set after it is bound cannot be moved by
editing either, so this job reconciles the label. It replaces the label, weekly-shard,
legacy-label and CSI-request tasks that `setup/k3s` ran from `tasks/longhorn.yml` and
`tasks/longhorn-weekly-shard.yml` (H1 plan, H1.6).

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

The steps run against each volume's own labels, in that order, and the run writes ONE merge
patch per changed volume: the net of the steps. A converged cluster writes nothing.

It then holds the memory REQUEST of the four `csi-*` Deployments that longhorn-driver-deployer
creates at runtime (`csi-attacher`, `csi-provisioner`, `csi-resizer`, `csi-snapshotter`). Upstream
ships them with `resources: {}`, which makes them BestEffort. A Deployment that already holds the
request is not patched, so a rollout happens only when the driver deployer has recreated one.

Inputs: `/config/tiers.json` (the ConfigMap's `{"r2": [...], "weekly": {claim: shard}, "nobackup":
[...]}`, claims as `namespace/pvcName`) and the environment: `RECONCILE_NAMESPACE`,
`RECONCILE_CSI_MEMORY_REQUEST`, and optionally `RECONCILE_TIERS_FILE` and `RECONCILE_CSI_WAIT_SECONDS`.
The `r2` list is read and validated but not acted on until H1.7a adds the `backupTargetName` arm.

It talks to the API server with the pod's ServiceAccount token, and the API server's CA reaches
`urlopen` through `SSL_CERT_FILE`, which the CronJob sets. Every connection goes through
`flux_job_connect`.

`--report-only` makes the reads and prints what a run WOULD change, with no write. It is the way
to check a change against a live cluster: run it with a read-only credential and expect no
`would patch` line on a converged cluster.

Exit 0 when every volume and every Deployment holds its desired state. Exit 1 when a read fails
(before any write), a claim is on two tier lists, or a write fails or a CSI Deployment is still
absent when the wait ends. `-h` and `--help` print this text.

Usage:

    python3 /config/longhorn_reconcile.py [--report-only]
"""

import argparse
import json
import os
import sys
import time
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

CSI_DEPLOYMENTS = ("csi-attacher", "csi-provisioner", "csi-resizer", "csi-snapshotter")
# The pause between two reads of a Deployment that is not there yet, in seconds.
CSI_PAUSE_SECONDS = 10
# How long a run waits for a CSI Deployment that does not exist yet. The Ansible task retried
# 12 times at 10 seconds, after a rollout wait on the driver deployer that this run no longer has.
CSI_WAIT_SECONDS = 120

MERGE_PATCH = "application/merge-patch+json"
STRATEGIC_PATCH = "application/strategic-merge-patch+json"


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
        `application/json` with 415. A Deployment's `containers` list needs the strategic type,
        because a JSON merge patch replaces the whole array.
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
        Failed: The file is unreadable or is not `{"r2": list, "weekly": {claim: 0-6}, "nobackup": list}`,
            or one claim sits on both the weekly and the no-backup list.
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
    return {"r2": r2, "weekly": weekly, "nobackup": set(nobackup)}


def claim_of(volume: dict) -> str:
    """`namespace/pvcName` of the PVC a Volume CR serves, or `/` when it serves none."""
    kubernetes = (volume.get("status") or {}).get("kubernetesStatus") or {}
    return f"{kubernetes.get('namespace', '')}/{kubernetes.get('pvcName', '')}"


def desired_labels(
    labels: Mapping[str, str], claim: str, tiers: Mapping, class_claims: set[str]
) -> dict[str, str]:
    """`labels` after the reconcile steps (module docstring) for the volume serving `claim`.

    Only the group labels move. Every other label is returned as it was.
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
    return state


def group_keys() -> set[str]:
    """Every group label key this job writes or removes."""
    groups = [DEFAULT, NO_BACKUP, LEGACY_WEEKLY, *(shard_group(s) for s in SHARDS)]
    return {label(group) for group in groups}


def plan(volumes: list[dict], class_claims: set[str], tiers: Mapping) -> list[dict]:
    """One merge patch per volume whose group labels must change, in volume-name order.

    Each entry is `{"name", "claim", "labels"}`, where `labels` maps a group label key to its
    new value, or to None to remove it.
    """
    keys = group_keys()
    out = []
    for volume in sorted(volumes, key=lambda v: v["metadata"]["name"]):
        before = volume["metadata"].get("labels") or {}
        claim = claim_of(volume)
        after = desired_labels(before, claim, tiers, class_claims)
        delta = {}
        for key in sorted(keys):
            if after.get(key) != before.get(key):
                delta[key] = after.get(key)
        if delta:
            out.append(
                {"name": volume["metadata"]["name"], "claim": claim, "labels": delta}
            )
    return out


def describe(change: dict) -> str:
    """One report line for a planned label patch."""
    parts = [
        f"{key.removeprefix(LABEL_PREFIX)}{'-' if value is None else '=' + value}"
        for key, value in change["labels"].items()
    ]
    return f"volume {change['name']} ({change['claim']}): {' '.join(parts)}"


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
    """Plan the label changes from two list reads, then write them. One report line per change.

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
            body = {"metadata": {"labels": change["labels"]}}
            status, answer = kube.call(
                "PATCH", f"{volumes_path}/{change['name']}", body, MERGE_PATCH
            )
            _ok(status, answer, f"patching volume {change['name']}")
        lines.append(("would patch " if report_only else "patched ") + describe(change))
    lines.append(
        f"{len(volumes)} volume(s) read, {len(changes)} label change(s)"
        f"{' planned' if report_only else ''}"
    )
    return lines


def sidecar_patch(name: str, request: str) -> dict:
    """The strategic merge patch that sets `name`'s memory request, naming its one container."""
    return {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": name, "resources": {"requests": {"memory": request}}}
                    ]
                }
            }
        }
    }


def held_request(deployment: dict, name: str) -> str:
    """The memory request of `deployment`'s container called `name`, or an empty string."""
    containers = (
        ((deployment.get("spec") or {}).get("template") or {}).get("spec") or {}
    ).get("containers") or []
    for container in containers:
        if container.get("name") == name:
            return str(
                ((container.get("resources") or {}).get("requests") or {}).get(
                    "memory", ""
                )
            )
    return ""


def reconcile_sidecars(
    kube: Kube,
    namespace: str,
    request: str,
    report_only: bool,
    wait_seconds: float = CSI_WAIT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[str], list[str]]:
    """Hold the memory request of each CSI Deployment. Returns (report lines, problems).

    DECIDED: a Deployment that does not exist yet is waited for, up to `wait_seconds`, and then
    reported as a problem, so the run exits 1. longhorn-driver-deployer creates the four
    asynchronously after the manager is Ready, so on a first apply they can lag the CronJob's
    first run by seconds. The Ansible task retried 12 times at 10 seconds for the same reason.
    Skipping silently would let a cluster that never grows its CSI Deployments read as converged.
    A report-only run never waits.
    """
    lines, problems = [], []
    deadline = clock() + wait_seconds
    base = f"/apis/apps/v1/namespaces/{namespace}/deployments"
    for name in CSI_DEPLOYMENTS:
        while True:
            status, body = kube.call("GET", f"{base}/{name}")
            if status != 404 or report_only or clock() >= deadline:
                break
            sleep(CSI_PAUSE_SECONDS)
        if status == 404:
            problems.append(f"deployment {name} does not exist")
            continue
        try:
            _ok(status, body, f"deployment {name}")
        except Failed as err:
            problems.append(str(err))
            continue
        have = held_request(body, name)
        if have == request:
            lines.append(f"deployment {name} already requests {request}")
            continue
        if report_only:
            lines.append(
                f"would patch deployment {name}: memory request {have or 'unset'} -> {request}"
            )
            continue
        status, answer = kube.call(
            "PATCH", f"{base}/{name}", sidecar_patch(name, request), STRATEGIC_PATCH
        )
        try:
            _ok(status, answer, f"patching deployment {name}")
        except Failed as err:
            problems.append(str(err))
            continue
        lines.append(
            f"patched deployment {name}: memory request {have or 'unset'} -> {request}"
        )
    return lines, problems


def run(
    kube: Kube,
    env: Mapping[str, str],
    report_only: bool,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[str], list[str]]:
    """The whole reconcile. Returns (report lines, problems); a problem makes the run exit 1."""
    tiers = load_tiers(env.get("RECONCILE_TIERS_FILE", TIERS_FILE))
    namespace = env["RECONCILE_NAMESPACE"]
    lines = reconcile_labels(kube, namespace, tiers, report_only)
    sidecar_lines, problems = reconcile_sidecars(
        kube,
        namespace,
        env["RECONCILE_CSI_MEMORY_REQUEST"],
        report_only,
        wait_seconds=float(env.get("RECONCILE_CSI_WAIT_SECONDS", CSI_WAIT_SECONDS)),
        sleep=sleep,
        clock=clock,
    )
    return lines + sidecar_lines, problems


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
        lines, problems = run(
            kube, os.environ if env is None else env, args.report_only
        )
    except Failed as err:
        print(f"longhorn-reconcile: {err}", file=sys.stderr)
        return 1
    for line in lines:
        print(f"longhorn-reconcile: {line}")
    for problem in problems:
        print(f"longhorn-reconcile: PROBLEM: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
