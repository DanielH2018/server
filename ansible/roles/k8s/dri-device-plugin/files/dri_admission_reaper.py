#!/usr/bin/env python3
"""Delete the Failed pods the kubelet rejected at admission, once their ReplicaSet replaced them.

After a node reboot the kubelet re-admits the pods already bound to it, and it can do that
before this role's device plugin has re-registered `devic.es/dri`. A GPU pod admitted in that
window ends `Failed` with reason `UnexpectedAdmissionError` ("Allocate failed due to no healthy
devices present") or `OutOf<resource>`. Its ReplicaSet creates a replacement, which schedules
once the plugin is back, but nothing deletes the rejected pod: the kube-controller-manager's
`terminated-pod-gc-threshold` is 12500 pods, so the tombstone stays Failed and dashboards list
it as Down (#6367, tdarr after the 2026-10-25 reboot).

The `dri-admission-reaper` CronJob runs this in the workload namespace and in each add-on
namespace. In the workload namespace a pod is deleted only when all three hold:

- its phase is `Failed`;
- its reason is `UnexpectedAdmissionError` or starts with `OutOf`, so the kubelet refused it
  before any container started, and it has no logs to lose;
- a `ReplicaSet` owns it, so its controller has already replaced it. A Job's failed pod is the
  Job's record and ages out with the Job's `ttlSecondsAfterFinished`; an `Evicted` pod is
  evidence of memory or disk pressure. Neither is touched.

The add-on namespaces, `kube-system` and `longhorn-system`, run Jobs this repo does not render:
k3s's helm-install Jobs and Longhorn's backup Jobs. No `ttlSecondsAfterFinished` reaches them,
so a run the weekly reboot interrupts stays Failed (#6460). There a pod is deleted when it is a
workload-namespace tombstone, or when both of these hold:

- its phase is `Failed`;
- a `Job` owns it. The Job object keeps its failure count and conditions, so the failure stays
  on record, and the pod's output is in Loki. The Job controller counts a pod before it lets the
  pod go, through its `batch.kubernetes.io/job-tracking` finalizer, so a delete cannot hide a
  failure from the Job's backoff.

The workload namespace keeps its Job pods, because the health readers read their logs.

Each delete carries a `uid` precondition, so a pod recreated under the same name in between is
left alone. A 404 means another actor deleted it first, which is fine.

It talks to the API server with the pod's ServiceAccount token, which may `list` and `delete`
pods in each namespace it reaps (`templates/admission-reaper-rbac.yaml.j2`), and the API server's CA
reaches `urlopen` through `SSL_CERT_FILE`, which the CronJob sets. Every connection goes
through `flux_job_connect`.

Usage:

    python3 /jobs/dri_admission_reaper.py

`REAPER_NAMESPACE` names the workload namespace. `REAPER_JOB_NAMESPACES` names the add-on
namespaces, separated by spaces; unset or empty means none. Exit 0 means every matching pod is
gone, and one stdout line per namespace counts them. Exit 1 means the list or a delete failed. `-h` and `--help` print this
text and exit 0.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
from collections.abc import Callable, Mapping
from pathlib import Path

from flux_job_connect import Request, open_url

API = "https://kubernetes.default.svc"
TOKEN_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/token"
# The seconds each API call may take. `open_url` retries a refused connect on top.
CALL_TIMEOUT = 10
# The kubelet's admission-failure reasons: the device manager's, and the resource fit's
# `OutOf<resource>` (pkg/kubelet/lifecycle/predicate.go).
ADMISSION_REASON = "UnexpectedAdmissionError"
OUT_OF_PREFIX = "OutOf"


def is_failed_job_pod(pod: Mapping) -> bool:
    """Whether `pod` is a Failed pod that a Job owns."""
    status = pod.get("status") or {}
    owners = (pod.get("metadata") or {}).get("ownerReferences") or []
    return status.get("phase") == "Failed" and any(
        owner.get("kind") == "Job" for owner in owners
    )


def is_addon_debris(pod: Mapping) -> bool:
    """Whether `pod` is a pod to delete in an add-on namespace."""
    return is_tombstone(pod) or is_failed_job_pod(pod)


class Failed(Exception):
    """An API call failed; the message says which and why."""


def is_tombstone(pod: Mapping) -> bool:
    """Whether `pod` is a ReplicaSet's pod that the kubelet rejected at admission."""
    status = pod.get("status") or {}
    reason = status.get("reason") or ""
    owners = (pod.get("metadata") or {}).get("ownerReferences") or []
    return (
        status.get("phase") == "Failed"
        and (reason == ADMISSION_REASON or reason.startswith(OUT_OF_PREFIX))
        and any(owner.get("kind") == "ReplicaSet" for owner in owners)
    )


class Kube:
    """The API server, as the two calls this job makes.

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
        self, method: str, path: str, body: Mapping | None = None
    ) -> tuple[int, dict]:
        """`method` on `path`, with `body` as JSON when given; the status and parsed reply."""
        request = Request(
            self.base + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method=method,
        )
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


def _checked(
    kube: Kube, what: str, method: str, path: str, body=None
) -> tuple[int, dict]:
    try:
        return kube.call(method, path, body)
    except OSError as err:
        raise Failed(f"{what} failed: {err}") from err


def failed_pods(kube: Kube, namespace: str) -> list[dict]:
    """The namespace's pods in phase `Failed`."""
    query = urllib.parse.urlencode({"fieldSelector": "status.phase=Failed"})
    path = f"/api/v1/namespaces/{namespace}/pods?{query}"
    status, body = _checked(kube, "listing Failed pods", "GET", path)
    if status != 200:
        raise Failed(
            f"listing Failed pods answered HTTP {status}: {body.get('message', '')}"
        )
    return list(body.get("items") or [])


def delete(kube: Kube, namespace: str, pod: Mapping) -> bool:
    """Delete `pod` if it is still the same object; whether this call removed it."""
    meta = pod.get("metadata") or {}
    name = meta.get("name")
    options = {
        "kind": "DeleteOptions",
        "apiVersion": "v1",
        "preconditions": {"uid": meta.get("uid")},
    }
    path = f"/api/v1/namespaces/{namespace}/pods/{name}"
    status, body = _checked(kube, f"deleting pod {name}", "DELETE", path, options)
    if status in (200, 202):
        return True
    # Gone already, or the name now belongs to a newer pod: nothing left to reap.
    if status in (404, 409):
        return False
    raise Failed(
        f"deleting pod {name} answered HTTP {status}: {body.get('message', '')}"
    )


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    kube: Kube | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.parse_args(argv)
    environ = os.environ if env is None else env
    rules = {environ["REAPER_NAMESPACE"]: is_tombstone}
    for namespace in environ.get("REAPER_JOB_NAMESPACES", "").split():
        rules[namespace] = is_addon_debris
    if kube is None:
        kube = Kube(API, Path(TOKEN_FILE).read_text().strip())
    # One namespace's failure does not stop the next: each is reaped on its own grant.
    code = 0
    for namespace, rule in rules.items():
        try:
            reap(kube, namespace, rule)
        except Failed as err:
            print(f"dri-admission-reaper: {err}", file=sys.stderr)
            code = 1
    return code


def reap(kube: Kube, namespace: str, rule: Callable[[Mapping], bool]) -> None:
    """Delete each Failed pod in `namespace` that `rule` matches, and print the count."""
    pods = failed_pods(kube, namespace)
    deleted = 0
    for pod in filter(rule, pods):
        if delete(kube, namespace, pod):
            deleted += 1
            meta, status = pod["metadata"], pod["status"]
            reason = status.get("reason") or "no reason"
            print(
                f"dri-admission-reaper: deleted {namespace}/{meta['name']} ({reason})"
            )
    print(
        f"dri-admission-reaper: deleted {deleted} of {len(pods)} Failed pods in {namespace}"
    )


if __name__ == "__main__":
    sys.exit(main())
