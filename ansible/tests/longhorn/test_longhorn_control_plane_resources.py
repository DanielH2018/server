#!/usr/bin/env python3
"""longhorn-manager must carry a memory REQUEST and no LIMIT.

Upstream's deploy/longhorn.yaml ships it at `resources: {}` — BestEffort QoS, oom_score_adj
1000 — so the kernel eats the storage control plane first under any memory pressure. The
manager's fix is the `longhorn` role's kustomize patch (H1.5a), which Flux applies with the
object it patches. The csi-* sidecars are not in the upstream file: longhorn-driver-deployer
creates them at runtime, and longhorn-manager sets their `resources` from the
`system-managed-csi-components-resource-limits` Setting the same role renders (#5958). The
Setting tests below hold it to the same rule. longhorn-global-manager, the Deployment v1.13
added, has its own patch and tests (#5968).

Only a request, never a limit: a limit set too low turns "killed by OOM badness" into "killed
by cgroup cap" — a different failure mode this design rejects. `test_a_patch_carrying_a_
limits_key_is_rejected` is the pair proving this guard can actually go red rather than
passing on any resources block it is handed.

Run: uv run pytest ansible/tests/longhorn/test_longhorn_control_plane_resources.py
"""

import json

import pytest
from _k8s_render import rendered_docs
from lib import yaml_fast

from lib.repo_paths import K8S_ROLES

LONGHORN_ROLE = K8S_ROLES / "longhorn"
LONGHORN_DEFAULT_VALUES = yaml_fast.safe_load(
    (LONGHORN_ROLE / "defaults" / "main.yml").read_text()
)
MANAGER_PATCH = LONGHORN_ROLE / "files" / "patches" / "longhorn-manager.yaml"
GLOBAL_MANAGER_PATCH = (
    LONGHORN_ROLE / "files" / "patches" / "longhorn-global-manager.yaml"
)
UPSTREAM = LONGHORN_ROLE / "files" / "upstream" / "longhorn.yaml"


def _container_resources(patch_doc):
    containers = patch_doc["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1, "patch must touch exactly one named container"
    return containers[0]["resources"]


def _assert_request_only(resources):
    """The shared assertion: a memory request, and nothing under `limits`."""
    assert "memory" in resources.get("requests", {}), (
        "resources block carries no memory request: %r" % resources
    )
    assert "limits" not in resources, (
        "resources block carries a limit (%r) — a limit set too low turns an OOM-by-badness "
        "kill into an OOM-by-cgroup-cap kill, which #1243's diagnosis explicitly rejected"
        % resources
    )


def _manager_patch() -> dict:
    return yaml_fast.safe_load(MANAGER_PATCH.read_text())


def _upstream_targets() -> set:
    return {
        (d["kind"], d["metadata"]["name"], d["metadata"].get("namespace"))
        for d in yaml_fast.safe_load_all(UPSTREAM.read_text())
        if isinstance(d, dict)
    }


def _target(patch: dict) -> tuple:
    return (patch["kind"], patch["metadata"]["name"], patch["metadata"]["namespace"])


def test_manager_patch_targets_the_daemonset_the_upstream_file_declares():
    # Kustomize refuses a build whose patch matches nothing, so a refresh that renames the
    # DaemonSet fails here before it fails the render.
    assert _target(_manager_patch()) in _upstream_targets()


def test_manager_patch_sets_a_request_and_no_limit():
    resources = _container_resources(_manager_patch())
    _assert_request_only(resources)


def test_manager_patch_holds_the_value_of_the_defaults_key():
    # A kustomize patch is copied verbatim and cannot read a variable, so the key in the
    # role's defaults is only as true as this test makes it.
    resources = _container_resources(_manager_patch())
    assert resources["requests"]["memory"] == str(
        LONGHORN_DEFAULT_VALUES["k3s_longhorn_manager_memory_request"]
    )


def test_manager_patch_names_the_longhorn_manager_container():
    containers = _manager_patch()["spec"]["template"]["spec"]["containers"]
    assert containers[0]["name"] == "longhorn-manager"


def test_a_patch_carrying_a_limits_key_is_rejected():
    """THE RED PROOF: _assert_request_only must fail closed on a resources block with a limit.

    Without this, the two accept-side tests above could pass by construction — the shared
    assertion could be checking nothing at all and every real patch would still read green.
    """
    with pytest.raises(AssertionError, match="limit"):
        _assert_request_only(
            {"requests": {"memory": "256Mi"}, "limits": {"memory": "256Mi"}}
        )


def test_a_patch_missing_a_request_is_rejected():
    with pytest.raises(AssertionError, match="memory request"):
        _assert_request_only({})


def _global_manager_patch() -> dict:
    return yaml_fast.safe_load(GLOBAL_MANAGER_PATCH.read_text())


def test_global_manager_patch_targets_the_deployment_the_upstream_file_declares():
    assert _target(_global_manager_patch()) == (
        "Deployment",
        "longhorn-global-manager",
        "longhorn-system",
    )
    assert _target(_global_manager_patch()) in _upstream_targets()


def test_global_manager_patch_names_the_global_manager_container():
    containers = _global_manager_patch()["spec"]["template"]["spec"]["containers"]
    assert containers[0]["name"] == "longhorn-global-manager"


def test_global_manager_patch_requests_the_defaults_value_and_no_limit():
    # fact: ansible/roles/k8s/longhorn/CLAUDE.md#At a glance
    # Upstream v1.13.0 ships the Deployment with no `resources`, so without the patch its
    # three pods run BestEffort (#5968).
    resources = _container_resources(_global_manager_patch())
    _assert_request_only(resources)
    assert resources["requests"]["memory"] == str(
        LONGHORN_DEFAULT_VALUES["k3s_longhorn_global_manager_memory_request"]
    )


def test_global_manager_patch_holds_the_replicas_of_the_defaults_key():
    # fact: ansible/roles/k8s/longhorn/CLAUDE.md#At a glance
    assert (
        _global_manager_patch()["spec"]["replicas"]
        == LONGHORN_DEFAULT_VALUES["k3s_longhorn_global_manager_replicas"]
    )


# The four Deployments longhorn-driver-deployer creates and the Setting names. Any other
# component in the Setting would have the manager roll it, and the plugin DaemonSet's roll can
# strand RWX mounts.
CSI_SIDECARS = {"csi-attacher", "csi-provisioner", "csi-resizer", "csi-snapshotter"}
CSI_SETTING = "system-managed-csi-components-resource-limits"


def _csi_setting_value() -> dict:
    """The parsed `value` of the CSI resource Setting in the `longhorn` role's render."""
    for role, _tpl, doc in rendered_docs():
        if (
            role == "longhorn"
            and doc.get("kind") == "Setting"
            and doc["metadata"]["name"] == CSI_SETTING
        ):
            assert isinstance(doc["value"], str), "a Setting's value is a string"
            return json.loads(doc["value"])
    raise AssertionError(
        "the longhorn render holds no system-managed-csi-components-resource-limits Setting; "
        "without it the CSI sidecars run BestEffort"
    )


def test_the_csi_setting_requests_the_defaults_value_for_the_four_sidecars_only():
    # fact: ansible/roles/k8s/longhorn/CLAUDE.md#At a glance
    # Exact equality, not a subset: longhorn-manager compares each entry with the live
    # container's `resources`, so an extra `limits: {}` never matches and rolls the Deployment
    # on every tick.
    request = {
        "requests": {
            "memory": str(
                LONGHORN_DEFAULT_VALUES["k3s_longhorn_csi_sidecar_memory_request"]
            )
        }
    }
    assert _csi_setting_value() == {name: request for name in CSI_SIDECARS}
