"""Cluster documents for the `k3s_gates` tests, and a fake API that serves them by path.

`healthy()` is a cluster every check passes on. A test overrides one route to make exactly one
check refuse, so each refusal has a rejecting fixture that differs from the accepting one in
one place.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

import k3s_gates

NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
VERSION = "v1.37.2+k3s1"
MUNGED = "v1.37.2-k3s1"


def stamp(delta_s: float) -> str:
    """A MicroTime `delta_s` seconds before NOW, as the API writes it."""
    return (NOW - timedelta(seconds=delta_s)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def listing(*items: dict) -> dict:
    return {"items": list(items)}


def named(name: str, **fields) -> dict:
    return {"metadata": {"name": name}, **fields}


def volume(name: str = "pvc-a", robustness: str | None = "healthy") -> dict:
    status = {} if robustness is None else {"robustness": robustness}
    return named(name, status=status)


def backup(name: str = "backup-a", state: str | None = "Completed") -> dict:
    return named(name, status={} if state is None else {"state": state})


def node(name: str, ready: str = "True", version: str = MUNGED, server: bool = False):
    labels = {k3s_gates.CONTROL_PLANE_LABEL: "true"} if server else {}
    return {
        "metadata": {"name": name, "labels": labels},
        "status": {
            "conditions": [{"type": "Ready", "status": ready}],
            "nodeInfo": {"kubeletVersion": version},
        },
    }


def coredns(**overrides) -> dict:
    status = {
        "observedGeneration": 4,
        "replicas": 2,
        "updatedReplicas": 2,
        "availableReplicas": 2,
    }
    status.update(overrides)
    return {"metadata": {"generation": 4}, "status": status}


def lease(
    acquired_s: float = 3600, renewed_s: float = 2, holder: str | None = "daniel-box_x"
) -> dict:
    spec = {
        "acquireTime": stamp(acquired_s),
        "renewTime": stamp(renewed_s),
        "leaseDurationSeconds": 15,
    }
    if holder is not None:
        spec["holderIdentity"] = holder
    return {"spec": spec}


def plan(name: str, applying=None, complete: str = "True", latest: str = MUNGED):
    status = {
        "latestVersion": latest,
        "conditions": [{"type": "Complete", "status": complete}],
    }
    if applying:
        status["applying"] = applying
    return named(name, status=status)


def job(name: str, condition: str | None = "Complete") -> dict:
    conditions = [] if condition is None else [{"type": condition, "status": "True"}]
    return named(name, status={"conditions": conditions})


def kustomization(name: str, **overrides) -> dict:
    item = {
        "metadata": {"name": name, "namespace": "flux-system", "generation": 3},
        "spec": {"sourceRef": {"kind": "OCIRepository", "name": "homelab"}},
        "status": {
            "observedGeneration": 3,
            "lastAppliedRevision": "prod@sha256:aaa",
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }
    for key, value in overrides.items():
        item[key] = value
    return item


SOURCE_PATH = (
    "/apis/source.toolkit.fluxcd.io/v1/namespaces/flux-system/ocirepositories/homelab"
)


def healthy() -> dict[str, dict]:
    """Every path a mode reads, serving a cluster that passes every check."""
    return {
        k3s_gates.VOLUMES_PATH: listing(volume()),
        k3s_gates.BACKUPS_PATH: listing(backup()),
        k3s_gates.NODES_PATH: listing(
            node("daniel-box", server=True), node("daniel-server")
        ),
        k3s_gates.COREDNS_PATH: coredns(),
        k3s_gates.LEASE_PATH: lease(),
        f"{k3s_gates.PLANS_PATH}/{k3s_gates.SERVER_PLAN}": plan("k3s-server"),
        f"{k3s_gates.PLANS_PATH}/{k3s_gates.AGENT_PLAN}": plan("k3s-agent"),
        k3s_gates.JOBS_PATH: listing(job("apply-k3s-server-abc")),
        k3s_gates.KUSTOMIZATIONS_PATH: listing(kustomization("homelab")),
        SOURCE_PATH: {"status": {"artifact": {"revision": "prod@sha256:aaa"}}},
    }


def fake_get(routes: Mapping[str, dict | Exception]):
    """A `get` that serves `routes`; a missing path raises ReadError, as a 404 does."""

    def get(path: str) -> dict:
        found = routes.get(path)
        if found is None:
            raise k3s_gates.ReadError(f"{path}: HTTP 404")
        if isinstance(found, Exception):
            raise found
        return found

    return get


def with_routes(**by_name) -> dict[str, dict]:
    """`healthy()` with named routes replaced: volumes=, backups=, nodes=, coredns=, lease=."""
    paths = {
        "volumes": k3s_gates.VOLUMES_PATH,
        "backups": k3s_gates.BACKUPS_PATH,
        "nodes": k3s_gates.NODES_PATH,
        "coredns": k3s_gates.COREDNS_PATH,
        "lease": k3s_gates.LEASE_PATH,
        "jobs": k3s_gates.JOBS_PATH,
        "kustomizations": k3s_gates.KUSTOMIZATIONS_PATH,
    }
    routes = healthy()
    for name, value in by_name.items():
        routes[paths[name]] = value
    return routes
