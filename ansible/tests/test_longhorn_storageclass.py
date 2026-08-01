#!/usr/bin/env python3
"""Guards that the Longhorn StorageClass never pins a replica count again.

The failure this encodes actually happened (daniel-box, 2026-08-01). The k3s role
patches `settings.longhorn.io default-replica-count` to k3s_longhorn_replica_count
(1, until daniel-server joins at slice 7), and that patch applied cleanly — reading
the setting back returned 1. But every PVC still bound at 3 replicas:

    kubectl -n longhorn-system get settings.longhorn.io default-replica-count  -> 1
    kubectl get sc longhorn -o jsonpath='{.parameters.numberOfReplicas}'       -> 3

Upstream's deploy/longhorn.yaml hardcodes `numberOfReplicas: "3"` in the
longhorn-storageclass ConfigMap, and a StorageClass parameter beats the global
setting. On a one-node cluster that means every volume asks for 3 replicas it can
never schedule and sits permanently Degraded — the exact "real fault buried in
expected noise" k3s_longhorn_replica_count exists to prevent. It failed slice 0's
exit criteria; see docs/k3s-migration/slice-0-cluster-foundation.md.

The fix replaces upstream's class with files/longhorn-storageclass.yaml, which omits
the parameter so the setting governs. The risk now is re-syncing that file from a
newer upstream and pasting the parameter back in, which is what these tests catch.

Run: uv run pytest ansible/tests/test_longhorn_storageclass.py
"""

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[1]
K3S = ANSIBLE / "roles" / "setup" / "k3s"
STORAGECLASS = K3S / "files" / "longhorn-storageclass.yaml"


def _tasks():
    return yaml.safe_load((K3S / "tasks" / "main.yml").read_text())


def _commands(tasks: list[dict]) -> list[str]:
    """The `cmd:` of every command task, positionally aligned with `tasks`."""
    return [t.get("ansible.builtin.command", {}).get("cmd", "") for t in tasks]


def _index_of(commands: list[str], matches) -> int:
    """Position of the first matching command, as a failed assert rather than a raise."""
    for i, cmd in enumerate(commands):
        if matches(cmd):
            return i
    raise AssertionError(
        "No k3s role task runs a command matching this predicate — a task was renamed "
        "or restructured, and the ordering guarantee below is no longer being checked."
    )


def test_storageclass_does_not_pin_a_replica_count():
    """The whole point of shipping our own class — see the module docstring."""
    sc = yaml.safe_load(STORAGECLASS.read_text())
    assert "numberOfReplicas" not in sc.get("parameters", {}), (
        "longhorn-storageclass.yaml must not set numberOfReplicas. A StorageClass "
        "parameter overrides the default-replica-count setting the role patches, so "
        "pinning it here silently ignores k3s_longhorn_replica_count."
    )


def test_storageclass_stays_the_cluster_default():
    """Dropping this annotation breaks every PVC that omits storageClassName."""
    sc = yaml.safe_load(STORAGECLASS.read_text())
    assert sc["metadata"]["name"] == "longhorn"
    assert sc["provisioner"] == "driver.longhorn.io"
    annotations = sc["metadata"].get("annotations", {})
    assert annotations.get("storageclass.kubernetes.io/is-default-class") == "true", (
        "longhorn must remain the default StorageClass — it replaces the one upstream "
        "marks default, so losing the annotation leaves the cluster with none."
    )


def test_role_still_patches_the_default_replica_count_setting():
    """With the parameter gone, the setting is the only lever left."""
    commands = _commands(_tasks())
    assert any("settings.longhorn.io default-replica-count" in c for c in commands), (
        "The role must keep patching default-replica-count. Since the StorageClass no "
        "longer pins numberOfReplicas, that setting is what k3s_longhorn_replica_count "
        "actually reaches volumes through."
    )


def test_storageclass_is_applied_after_upstream_longhorn():
    """Ordering is load-bearing: upstream's apply re-pins the ConfigMap every run."""
    commands = _commands(_tasks())
    install = _index_of(commands, lambda c: "deploy/longhorn.yaml" in c)
    apply_class = _index_of(
        commands, lambda c: "apply -f" in c and "longhorn-storageclass.yaml" in c
    )
    patch_configmap = _index_of(
        commands, lambda c: "patch configmap longhorn-storageclass" in c
    )
    assert install < apply_class, (
        "The StorageClass must be applied AFTER `kubectl apply -f longhorn.yaml`, "
        "which recreates upstream's class and ConfigMap."
    )
    assert install < patch_configmap, (
        "The ConfigMap patch must run AFTER `kubectl apply -f longhorn.yaml`, which "
        're-applies upstream\'s numberOfReplicas: "3" into it on every run.'
    )
