#!/usr/bin/env python3
"""longhorn-manager must carry a memory REQUEST and no LIMIT.

Upstream's deploy/longhorn.yaml ships it at `resources: {}` — BestEffort QoS, oom_score_adj
1000 — so the kernel eats the storage control plane first under any memory pressure. The
manager's fix is the `longhorn` role's kustomize patch (H1.5a), which Flux applies with the
object it patches. The csi-* sidecars are not in the upstream file: longhorn-driver-deployer
creates them at runtime, so the `longhorn-reconcile` job patches them (H1.6) and its tests,
`ansible/roles/k8s/longhorn-reconcile/tests/test_longhorn_reconcile.py`, hold the same rule.

Only a request, never a limit: a limit set too low turns "killed by OOM badness" into "killed
by cgroup cap" — a different failure mode this design rejects. `test_a_patch_carrying_a_
limits_key_is_rejected` is the pair proving this guard can actually go red rather than
passing on any resources block it is handed.

Run: uv run pytest ansible/tests/longhorn/test_longhorn_control_plane_resources.py
"""

import pytest
from lib import yaml_fast

from lib.repo_paths import K8S_ROLES

LONGHORN_ROLE = K8S_ROLES / "longhorn"
LONGHORN_DEFAULT_VALUES = yaml_fast.safe_load(
    (LONGHORN_ROLE / "defaults" / "main.yml").read_text()
)
MANAGER_PATCH = LONGHORN_ROLE / "files" / "patches" / "longhorn-manager.yaml"
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


def test_manager_patch_targets_the_daemonset_the_upstream_file_declares():
    # Kustomize refuses a build whose patch matches nothing, so a refresh that renames the
    # DaemonSet fails here before it fails the render.
    patch = _manager_patch()
    declared = {
        (d["kind"], d["metadata"]["name"], d["metadata"].get("namespace"))
        for d in yaml_fast.safe_load_all(UPSTREAM.read_text())
        if isinstance(d, dict)
    }
    assert (
        patch["kind"],
        patch["metadata"]["name"],
        patch["metadata"]["namespace"],
    ) in declared


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
