"""A rendered `system-upgrade` tree and the guest snapshots that judge it, for the SUC rehearsal tests.

The role does not exist on master when the rehearsal mode lands (H6.1b is a parallel PR), so the
tests build the tree `homelab_render.py cluster --role flux --role system-upgrade` would write
from the H6 plan's description of it. Used by
ansible/roles/setup/hypervisor/tests/test_suc_rehearsal.py and
ansible/tests/staging/test_suc_rehearsal_mode.py.
"""

import copy
from typing import Any
from pathlib import Path

import yaml

TARGET = "v9.9.9+k3s1"
DIGEST = "sha256:" + "ab" * 32
GUEST = "etcd-drill"

PLAN: dict[str, Any] = {
    "apiVersion": "upgrade.cattle.io/v1",
    "kind": "Plan",
    "metadata": {"name": "k3s-server", "namespace": "system-upgrade"},
    "spec": {
        "version": TARGET,
        "concurrency": 1,
        "serviceAccountName": "k3s-plan",
        "nodeSelector": {
            "matchExpressions": [
                {
                    "key": "node-role.kubernetes.io/control-plane",
                    "operator": "In",
                    "values": ["true"],
                },
                {
                    "key": "kubernetes.io/hostname",
                    "operator": "In",
                    "values": ["no-node-selected"],
                },
                {"key": "homelab/k3s-upgrade-paused", "operator": "DoesNotExist"},
            ]
        },
        "prepare": {"image": "gate", "args": ["server"]},
        "upgrade": {"image": "rancher/k3s-upgrade"},
    },
}
KUSTOMIZATION: dict[str, Any] = {
    "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
    "kind": "Kustomization",
    "metadata": {"name": "system-upgrade", "namespace": "flux-system"},
    "spec": {
        "path": "./cluster/system-upgrade",
        "sourceRef": {"kind": "OCIRepository", "name": "homelab"},
        "dependsOn": [{"name": "cluster-base"}],
        "postBuild": {"substituteFrom": [{"kind": "Secret", "name": "cluster-vars"}]},
        "healthCheckExprs": [
            {"apiVersion": "upgrade.cattle.io/v1", "kind": "Plan", "current": "true"}
        ],
        "timeout": "100m",
    },
}


def write_rendered(
    root: Path, *, plan=PLAN, kustomization=KUSTOMIZATION, components=True
) -> Path:
    """Write the tree under `root` and return it. Pass a modified `plan` or `kustomization` to break it."""
    bundle = root / "cluster" / "system-upgrade"
    bundle.mkdir(parents=True)
    (bundle / "plans.yaml").write_text(yaml.safe_dump_all([plan]))
    (bundle / "kustomization.yaml").write_text("resources: [plans.yaml]\n")
    (root / "flux").mkdir()
    (root / "flux" / "system-upgrade.yaml").write_text(yaml.safe_dump(kustomization))
    if components:
        (root / "cluster" / "flux").mkdir()
        (root / "cluster" / "flux" / "components.yaml").write_text("kind: Namespace\n")
    return root


def plan_without(*spec_keys) -> dict:
    """A deep copy of PLAN with the named `spec` keys removed."""
    plan = copy.deepcopy(PLAN)
    for key in spec_keys:
        plan["spec"].pop(key, None)
    return plan


def condition(kind, status, reason="", message=""):
    return {"type": kind, "status": status, "reason": reason, "message": message}


def snapshot(
    *,
    ready="True",
    reason="ReconciliationSucceeded",
    revision=f"rehearsal@{DIGEST}",
    plan=True,
    complete=None,
    label="hash1",
    latest="hash1",
    kubelet=TARGET,
) -> dict:
    """What `suc-rehearsal-guest-run snapshot` prints. The defaults are a finished upgrade."""
    ks = {
        "status": {
            "lastAttemptedRevision": revision,
            "conditions": [condition("Ready", ready, reason, "a message")],
        }
    }
    plan_obj = (
        {
            "status": {
                "latestHash": latest,
                "conditions": complete or [condition("Complete", "True")],
            }
        }
        if plan
        else None
    )
    node = {
        "metadata": {"labels": {"plan.upgrade.cattle.io/k3s-server": label}},
        "status": {"nodeInfo": {"kubeletVersion": kubelet}},
    }
    return {"kustomization": ks, "plan": plan_obj, "node": node}


def red_snapshot(**changes) -> dict:
    """A Plan whose Job failed under a Kustomization that reads not Ready."""
    base = {
        "ready": "False",
        "reason": "HealthCheckFailed",
        "complete": [condition("Complete", "False", "JobFailed")],
        "label": "",
        "latest": "hash1",
        "kubelet": "v1.0.0+k3s1",
    }
    return snapshot(**{**base, **changes})
