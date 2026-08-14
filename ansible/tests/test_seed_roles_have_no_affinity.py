#!/usr/bin/env python3
"""A role that seeds a volume must not steer its workload toward a node.

THE COLLISION. `k8s/seed-volume` creates its seed pod with no nodeSelector and no
affinity, so the scheduler puts it on either node. That pod mounts the service's RWO
Longhorn claim — which, if the workload is already running, is attached to whichever node
the workload sits on. Land the two on different nodes and the seed pod wedges in
ContainerCreating with

    Multi-Attach error for volume "pvc-..." Volume is already used by pod(s) ...

until the deploy's 180s wait gives up and fails the play. Observed 2026-08-14 on prowlarr
during a full deploy.

This was survivable while every workload lived on one node. Once daniel-server joined
(2026-08-13) and workloads spread across both, the placement became a coin flip on every
deploy. Adding a node preference to a seeding role loads that coin — which is why the nine
seeding roles carry a priorityClassName and no affinity.

This guards the workaround, not the underlying bug: seed-volume's unconstrained seed pod
is the real defect and is still open. If it gains a constraint that follows the claim (or
quiesces the workload), this test should be deleted, not worked around.

Run: uv run pytest ansible/tests/test_seed_roles_have_no_affinity.py
"""

from pathlib import Path

import pytest

K8S_ROLES = Path(__file__).resolve().parents[1] / "roles" / "k8s"


def _seeding_roles():
    roles = []
    for role_dir in sorted(K8S_ROLES.iterdir()):
        tasks = role_dir / "tasks" / "main.yml"
        if tasks.is_file() and "k8s/seed-volume" in tasks.read_text():
            roles.append(role_dir.name)
    return roles


def test_seeding_roles_are_discovered():
    """Guard against the discovery logic silently matching nothing."""
    assert _seeding_roles(), (
        "found no roles including k8s/seed-volume — check the matcher"
    )


@pytest.mark.parametrize("role", _seeding_roles())
def test_seeding_role_has_no_node_affinity(role):
    for tpl in sorted((K8S_ROLES / role / "templates").glob("*.j2")):
        text = tpl.read_text()
        assert "preferredDuringSchedulingIgnoredDuringExecution" not in text, (
            f"{role}/{tpl.name} includes k8s/seed-volume and also carries a node affinity. "
            f"The seed pod is scheduled without constraints, so steering the workload toward "
            f"one node makes a Multi-Attach collision on its RWO claim likelier — the deploy "
            f"then fails waiting for a seed pod that can never start. Keep the "
            f"priorityClassName; drop the affinity."
        )
