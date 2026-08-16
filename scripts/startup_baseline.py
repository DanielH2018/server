#!/usr/bin/env python3
"""Container-start to pod-Ready for every workload, read from the live cluster.

The second half of a `Recreate` downtime gap is (new pod starting -> Ready), and Kubernetes
already records it on every running pod. That makes this a fleet-wide baseline with no deploy
required, where measuring the gap directly would need one rollout per service — and a rollout
only happens when a deploy actually changes a rendered manifest.

What it does NOT measure is the termination half. A pod that ignores SIGTERM burns its full
`terminationGracePeriodSeconds` on top of these numbers, and only a real rollout shows that.

Two traps this encodes, both hit while writing it:

  * Measure from the CURRENT container start, never `creationTimestamp`. A pod that restarted
    has a creation timestamp days older than its running container, which read as a 31-hour
    startup on the first attempt and poisoned every aggregate.
  * A container with no `readinessProbe` is marked Ready the moment it starts, so it reports
    ~0s. That is the absence of a measurement, not a fast one, and averaging it in flatters
    the fleet. Those workloads are counted separately.

Usage: uv run python scripts/startup_baseline.py [namespace]
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def startup_seconds(pod: dict) -> float | None:
    """Seconds from the last container start to the pod reporting Ready, or None.

    None when the pod is not Running, has no started container, has never gone Ready, or is
    owned by a Job (one-shot probes and reconcilers are not workloads whose rollout gap
    matters).
    """
    if pod.get("status", {}).get("phase") != "Running":
        return None
    owners = pod.get("metadata", {}).get("ownerReferences") or [{}]
    if owners[0].get("kind") == "Job":
        return None

    starts = [
        _parse(cs["state"]["running"]["startedAt"])
        for cs in pod.get("status", {}).get("containerStatuses", [])
        if cs.get("state", {}).get("running", {}).get("startedAt")
    ]
    ready = next(
        (
            c
            for c in pod.get("status", {}).get("conditions", [])
            if c["type"] == "Ready"
            and c["status"] == "True"
            and c.get("lastTransitionTime")
        ),
        None,
    )
    if not starts or not ready:
        return None

    secs = (_parse(ready["lastTransitionTime"]) - max(starts)).total_seconds()
    return None if secs < 0 else secs


def has_readiness_probe(pod: dict) -> bool:
    return any("readinessProbe" in c for c in pod.get("spec", {}).get("containers", []))


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    namespace = args[0] if args else "homelab"

    result = subprocess.run(
        ["kubectl", "-n", namespace, "get", "pods", "-o", "json"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        print(f"kubectl failed: {result.stderr.strip()}")
        return 1

    rows = []
    for pod in json.loads(result.stdout)["items"]:
        secs = startup_seconds(pod)
        if secs is None:
            continue
        name = pod["metadata"]["name"].rsplit("-", 2)[0]
        rows.append((secs, name, has_readiness_probe(pod)))

    if not rows:
        print(f"no running workloads found in namespace {namespace}")
        return 1

    rows.sort(reverse=True)
    print(f"{'start->ready':>13}  probe  service")
    for secs, svc, probed in rows:
        print(f"{secs:12.0f}s  {'yes' if probed else 'NO':>5}  {svc}")

    probed = sorted(r[0] for r in rows if r[2])
    unprobed = sum(1 for r in rows if not r[2])
    n = len(probed)
    print(f"\nworkloads         : {len(rows)}  ({n} with a readinessProbe)")
    if n:
        print(f"median (probed)   : {probed[n // 2]:.0f}s")
        print(f"p90 (probed)      : {probed[min(int(n * 0.9), n - 1)]:.0f}s")
        print(f"max (probed)      : {probed[-1]:.0f}s")
        print(f"over 30s          : {sum(1 for v in probed if v > 30)}")
    print(
        f"no readinessProbe : {unprobed}  (their ~0s is an absent measurement, not a fast one)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
