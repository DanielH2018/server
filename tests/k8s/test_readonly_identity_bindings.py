"""The read-only identity row finds a binding by every subject form that reaches the identity.

`_rbac_property_rows.py` holds the row. Its red fixtures test the predicate, so this file runs
the selector: a binding that names the identity as a ServiceAccount, or by either group that
contains it, is read, and a binding of any other identity is not.

Run: uv run pytest tests/k8s/test_readonly_identity_bindings.py
"""

from dataclasses import replace

from _property_table import check
from _rbac_property_rows import (
    RBAC_PROPERTIES,
    READONLY_NS,
    READONLY_SA,
    binding_doc,
)

ROW = RBAC_PROPERTIES[0]
IDENTITY = {"kind": "ServiceAccount", "name": READONLY_SA, "namespace": READONLY_NS}


def _docs(*bindings):
    return [("rogue-role", "binding.yaml.j2", b) for b in bindings]


def _problems(*bindings):
    return check(replace(ROW, min_matches=1, must_find=frozenset()), _docs(*bindings))


def test_a_cluster_admin_binding_of_the_identity_from_another_role_is_flagged():
    problems = _problems(
        binding_doc(
            "ClusterRoleBinding",
            "fake-admin",
            "ClusterRole",
            "cluster-admin",
            [IDENTITY],
        )
    )
    assert any("'cluster-admin'" in p for p in problems), problems


def test_a_group_that_contains_the_identity_is_read_by_either_group_form():
    for group in ("system:serviceaccounts", f"system:serviceaccounts:{READONLY_NS}"):
        problems = _problems(
            binding_doc(
                "ClusterRoleBinding",
                "fake-group",
                "ClusterRole",
                "edit",
                [{"kind": "Group", "name": group}],
            )
        )
        assert any("'edit'" in p for p in problems), (group, problems)


def test_a_binding_of_another_identity_is_not_selected():
    other = (
        binding_doc(
            "ClusterRoleBinding",
            "fake-other",
            "ClusterRole",
            "cluster-admin",
            [
                {"kind": "ServiceAccount", "name": "fake-sa", "namespace": READONLY_NS},
                {"kind": "ServiceAccount", "name": READONLY_SA, "namespace": "fake-ns"},
                {"kind": "Group", "name": "system:serviceaccounts:fake-ns"},
                {"kind": "User", "name": READONLY_SA},
            ],
        ),
    )
    assert _problems(*other) == [
        f"{ROW.name}: subject gone: delete this row (the selector matched nothing)"
    ]
