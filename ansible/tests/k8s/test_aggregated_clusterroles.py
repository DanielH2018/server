"""ClusterRoles that aggregate into `view`, `edit` or `admin`, and what the one in `view` grants.

A ClusterRole labelled `aggregate-to-view` adds its rules to `view`, which the read-only
identity and Headlamp bind, without touching either binding. Flux's vendored install ships
`flux-view-flux-system` that way, so every identity bound to `view` reads every Flux kind. The
operator accepted that on 2026-10-12 (#4742). These guards make the next such grant a decision:

- every aggregating ClusterRole across the render is listed with a reason, and the list names
  only roles the render still holds;
- `flux-view-flux-system` stays read-only on exactly six Flux groups, so a regeneration of the
  vendored file that widens it fails here rather than widening `view` silently.

Run: uv run pytest ansible/tests/k8s/test_aggregated_clusterroles.py
"""

import copy

from _k8s_render import rendered_docs
from test_k8s_manifests_rbac import _grant_violations, _readonly_rbac_docs

AGGREGATE_LABELS = frozenset(
    f"rbac.authorization.k8s.io/aggregate-to-{role}"
    for role in ("view", "edit", "admin")
)

# Every ClusterRole that aggregates into a built-in role, and why it may. The census below
# must find exactly these names, so a regenerated vendored file that adds or drops one fails.
AGGREGATED_CLUSTERROLES = {
    "flux-view-flux-system": (
        "Flux's vendored read on its own API groups. It reaches `view`, so the read-only "
        "identity and Headlamp read every Flux kind; the operator accepted that on "
        "2026-10-12 (#4742), and the grant tests below pin what it holds."
    ),
    "flux-edit-flux-system": (
        "Flux's vendored write on its own API groups. It aggregates into `edit` and `admin` "
        "only, and no binding in the tree names either, which the census also checks."
    ),
}

# The built-in roles `flux-edit-flux-system` widens with Flux writes. A binding to one of them
# hands its subject those writes, so it voids that row's reason.
WRITING_AGGREGATES = frozenset({"edit", "admin"})


def _cluster_rbac_docs() -> list[dict]:
    """Every rendered k8s manifest, plus the setup role's read-only RBAC."""
    return [doc for _role, _tpl, doc in rendered_docs()] + _readonly_rbac_docs()


def _aggregating_clusterroles(docs: list[dict]) -> dict[str, dict]:
    """The ClusterRoles carrying any aggregate label, by name, whatever the label's value."""
    return {
        doc["metadata"]["name"]: doc
        for doc in docs
        if doc.get("kind") == "ClusterRole"
        and AGGREGATE_LABELS & set(doc["metadata"].get("labels") or {})
    }


def _aggregation_problems(docs: list[dict]) -> list[str]:
    """Every aggregated grant the allowlist does not account for. Empty means it holds."""
    found = set(_aggregating_clusterroles(docs))
    problems = [
        f"unlisted aggregating ClusterRole {n}"
        for n in sorted(found - set(AGGREGATED_CLUSTERROLES))
    ]
    problems += [
        f"listed but not rendered: {n}"
        for n in sorted(set(AGGREGATED_CLUSTERROLES) - found)
    ]
    problems += [
        f"{doc['metadata']['name']} binds '{doc['roleRef']['name']}'"
        for doc in docs
        if doc.get("kind") in {"ClusterRoleBinding", "RoleBinding"}
        and doc["roleRef"]["name"] in WRITING_AGGREGATES
    ]
    return problems


def test_every_aggregated_clusterrole_is_listed_with_a_reason():
    docs = _cluster_rbac_docs()
    assert "flux-view-flux-system" in _aggregating_clusterroles(docs), (
        "the census no longer finds Flux's view role — check the flux render"
    )
    assert _aggregation_problems(docs) == []


def test_the_aggregation_check_flags_an_unlisted_role_and_a_writing_binding():
    """The rejecting half: an unlisted `aggregate-to-view` role, and a binding to `edit`."""
    rogue = {
        "kind": "ClusterRole",
        "metadata": {
            "name": "rogue-view",
            "labels": {"rbac.authorization.k8s.io/aggregate-to-view": "true"},
        },
        "rules": [],
    }
    binding = {
        "kind": "RoleBinding",
        "metadata": {"name": "rogue-edit"},
        "roleRef": {"kind": "ClusterRole", "name": "edit"},
    }
    docs = _cluster_rbac_docs()
    assert _aggregation_problems([*docs, rogue]) == [
        "unlisted aggregating ClusterRole rogue-view"
    ]
    assert _aggregation_problems([*docs, binding]) == ["rogue-edit binds 'edit'"]


# The groups `flux-view-flux-system` reads, as Flux v2.9.6 ships it. A regeneration that adds
# a group widens `view` for every identity bound to it, so it fails here and is decided here.
FLUX_VIEW_GROUPS = frozenset(
    {
        "kustomize.toolkit.fluxcd.io",
        "source.toolkit.fluxcd.io",
        "notification.toolkit.fluxcd.io",
        "helm.toolkit.fluxcd.io",
        "image.toolkit.fluxcd.io",
        "source.extensions.fluxcd.io",
    }
)


def _flux_view_problems(role: dict) -> list[str]:
    """Every way `role` exceeds read on the six Flux groups. Empty means it holds."""
    groups = {g for rule in role["rules"] for g in rule.get("apiGroups", [])}
    problems = _grant_violations(role["rules"])
    if groups != FLUX_VIEW_GROUPS:
        problems.append(f"groups differ by {sorted(groups ^ FLUX_VIEW_GROUPS)}")
    return problems


def _flux_view_role() -> dict:
    return _aggregating_clusterroles(_cluster_rbac_docs())["flux-view-flux-system"]


def test_flux_view_role_reads_the_six_flux_groups_only():
    role = _flux_view_role()
    groups = {g for rule in role["rules"] for g in rule.get("apiGroups", [])}
    # Exact match, not `in`: see ansible/tests/repo/test_census_rows_python.py (row `no-host-shaped-membership-literal`)
    assert any(g == "kustomize.toolkit.fluxcd.io" for g in groups), (
        "no Flux group parsed from flux-view-flux-system"
    )
    assert _flux_view_problems(role) == []


def test_the_flux_view_check_flags_an_added_group_and_a_write_verb():
    widened = copy.deepcopy(_flux_view_role())
    widened["rules"][0]["apiGroups"].append("apps")
    assert _flux_view_problems(widened) == ["groups differ by ['apps']"]
    writing = copy.deepcopy(_flux_view_role())
    writing["rules"][0]["verbs"].append("create")
    assert _flux_view_problems(writing), (
        "a create verb on the Flux groups was not caught"
    )
