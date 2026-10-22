"""The read-only identity's name and namespace are a mirrored pair across two roles (H1.2).

`setup/k3s` writes the kubeconfig and the audit policy for the `homelab-readonly`
ServiceAccount, and reads `k3s_readonly_sa_name` and `k3s_readonly_sa_namespace`. The
`cluster-base` role renders that ServiceAccount, and reads `cluster_base_readonly_sa_name` and
`cluster_base_readonly_sa_namespace`. A k8s role's defaults are invisible to a setup role and the
reverse, so there are two keys per value. One that drifts leaves a kubeconfig that
authenticates as an account the cluster does not have.

Run: uv run pytest ansible/tests/k8s/test_cluster_base_mirrors_the_k3s_identity.py
"""

import pytest
from lib import yaml_fast
from lib.repo_paths import K3S_DEFAULTS, K8S_ROLES

CLUSTER_BASE_DEFAULTS = K8S_ROLES / "cluster-base" / "defaults" / "main.yml"

# (the key in setup/k3s's defaults, its twin in cluster-base's defaults)
MIRRORED_PAIRS = (
    ("k3s_readonly_sa_name", "cluster_base_readonly_sa_name"),
    ("k3s_readonly_sa_namespace", "cluster_base_readonly_sa_namespace"),
)


def unequal_pairs(k3s: dict, cluster_base: dict) -> list[str]:
    """One line for each mirrored pair whose two values differ, or whose key is missing."""
    problems = []
    for k3s_key, base_key in MIRRORED_PAIRS:
        if k3s_key not in k3s or base_key not in cluster_base:
            problems.append(f"{k3s_key} / {base_key}: a key of the pair is not defined")
        elif k3s[k3s_key] != cluster_base[base_key]:
            problems.append(
                f"{k3s_key}={k3s[k3s_key]!r} but {base_key}={cluster_base[base_key]!r}"
            )
    return problems


def test_each_mirrored_pair_is_equal():
    # fact: ansible/roles/k8s/cluster-base/CLAUDE.md#At a glance
    k3s = yaml_fast.safe_load(K3S_DEFAULTS.read_text())
    cluster_base = yaml_fast.safe_load(CLUSTER_BASE_DEFAULTS.read_text())
    assert unequal_pairs(k3s, cluster_base) == []


def test_a_pair_with_one_side_renamed_is_flagged():
    k3s = {"k3s_readonly_sa_name": "homelab-readonly", "k3s_readonly_sa_namespace": "x"}
    base = {
        "cluster_base_readonly_sa_name": "homelab-reader",
        "cluster_base_readonly_sa_namespace": "x",
    }
    assert len(unequal_pairs(k3s, base)) == 1


@pytest.mark.parametrize("side", ["k3s", "cluster_base"])
def test_a_pair_missing_a_key_is_flagged(side):
    k3s = {"k3s_readonly_sa_name": "a", "k3s_readonly_sa_namespace": "b"}
    base = {
        "cluster_base_readonly_sa_name": "a",
        "cluster_base_readonly_sa_namespace": "b",
    }
    del (k3s if side == "k3s" else base)[
        "k3s_readonly_sa_name" if side == "k3s" else "cluster_base_readonly_sa_name"
    ]
    assert unequal_pairs(k3s, base) != []
