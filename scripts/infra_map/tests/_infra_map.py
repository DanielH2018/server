"""The inventory, role index and record builders the gen_infra_map suites share.

Every record here is a plain dict shaped like the collector's output, built by a helper
with defaults, so a test states only the field it is about. Nothing here needs an age key,
a cluster, or ssh.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

# The module under test lives one directory up; pytest resolves it through `pythonpath`,
# and this bootstrap keeps the import honest outside pytest (the bootstrap guard checks).
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import gen_infra_map as g


REPO_ROOT = g.REPO_ROOT

GLOBALS = {
    "k8s_hostname_suffix": "-k8s",
    "k8s_namespace": "homelab",
    "k8s_observability_namespace": "observability",
}

ROLES = g.RoleIndex(
    container_owners={
        "node-exporter": "prometheus",
        "cadvisor": "prometheus",
        "prometheus": "prometheus",
        "unbound": "pihole",
    },
    batch_roles=frozenset({"configarr", "n8n-images"}),
)


def docker_host(containers_list):
    return {"server_ip": "10.0.0.161", "containers_list": containers_list}


def live_ok(data):
    return {"ok": True, "data": data, "error": ""}


def container(state="running", status="Up 2 days (healthy)", image="img:1"):
    return {
        "state": state,
        "status": status,
        "image": image,
        "healthy": "(healthy)" in status,
        "unhealthy": "(unhealthy)" in status,
    }


def deployment(name, ns="homelab", ready=1, desired=1, image="img:1"):
    status = {"readyReplicas": ready} if ready else {}
    return {
        "metadata": {"name": name, "namespace": ns},
        "spec": {
            "replicas": desired,
            "template": {"spec": {"containers": [{"image": image}]}},
        },
        "status": status,
    }


def daemonset(name, ns="homelab", ready=2, desired=2, image="img:1"):
    """A DaemonSet carries no spec.replicas; its counts live only in status."""
    return {
        "kind": "DaemonSet",
        "metadata": {"name": name, "namespace": ns},
        "spec": {"template": {"spec": {"containers": [{"image": image}]}}},
        "status": {"desiredNumberScheduled": desired, "numberReady": ready},
    }


WORKLOADS = {
    ("homelab", "n8n"): {"ready": 1, "desired": 1, "image": "n8n:1"},
    ("homelab", "n8n-runners"): {"ready": 1, "desired": 1, "image": "n8n:1"},
    ("homelab", "n8nother"): {"ready": 1, "desired": 1, "image": "x:1"},
    ("observability", "loki"): {"ready": 1, "desired": 1, "image": "loki:1"},
    ("observability", "tempo"): {"ready": 1, "desired": 1, "image": "tempo:1"},
}


def service(name="sonarr", **overrides):
    base = {
        "name": name,
        "platform": "docker",
        "hostname": None,
        "port": None,
        "authelia": False,
        "networks": [],
        "namespace": None,
        "declared": True,
        "status": "unknown",
        "detail": "",
        "image": "",
        "replicas": None,
    }
    return {**base, **overrides}


def model_for(live, cluster=None):
    host_vars = {
        "daniel-box": docker_host(
            [{"name": "traefik", "platform": "k8s", "port": 8080}]
        ),
        "daniel-server": docker_host([]),
        "daniel-pi": docker_host([{"name": "sonarr", "port": 8989}]),
    }
    return g.build_model(
        GLOBALS, host_vars, live, "2026-08-07 03:00 CDT", ROLES, cluster
    )


def node(name, ready=True, roles=(), ip="10.0.0.1"):
    return {
        "metadata": {
            "name": name,
            "labels": {f"node-role.kubernetes.io/{r}": "true" for r in roles},
        },
        "spec": {},
        "status": {
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
            "addresses": [{"type": "InternalIP", "address": ip}],
            "nodeInfo": {"kubeletVersion": "v1.36.2+k3s1"},
        },
    }


def backup_target(name, url="s3://b@r/p", available=True):
    return {
        "metadata": {"name": name},
        "spec": {"backupTargetURL": url},
        "status": {"available": available},
    }


PODS = [
    {
        "namespace": "homelab",
        "name": "sonarr-7d9-a",
        "node": "daniel-server",
        "phase": "Running",
    },
    {
        "namespace": "homelab",
        "name": "sonarr-7d9-b",
        "node": "daniel-box",
        "phase": "Running",
    },
    {
        "namespace": "observability",
        "name": "sonarr-7d9-c",
        "node": "daniel-box",
        "phase": "Running",
    },
    {
        "namespace": "homelab",
        "name": "other-1-d",
        "node": "daniel-box",
        "phase": "Running",
    },
]
