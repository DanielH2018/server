"""The first PR of *Retiring a flux-owned service* in docs/flux-deploy-plane.md, rendered.

headlamp stands in for a retiring service with a deferred file. `<tag>-deferred` keeps
`deletionPolicy: Orphan`, so only its inventory prune deletes its objects, and that needs the
Kustomization to outlive them.

Run: uv run pytest scripts/validate/tests/test_validate_cross_plane_retirement.py
"""

from pathlib import Path

import pytest

from validate import render_diff_gates as gate

ROLES = ("headlamp",)
ROLE = "k8s/roles/headlamp"
DEFERRED = "headlamp-deferred"
TEMPLATE = "rbac-observability.yaml.j2"


@pytest.fixture(scope="module")
def head_snapshot(tmp_path_factory) -> gate.Snapshot:
    tree = tmp_path_factory.mktemp("head")
    gate.extract("HEAD", tree, gate.RENDER_TREES)
    return gate.snapshot(tree, ROLES)


def test_an_emptied_deferred_template_keeps_the_kustomization(tmp_path, head_snapshot):
    gate.extract("HEAD", tmp_path, gate.RENDER_TREES)
    (tmp_path / ROLE / "templates" / TEMPLATE).write_text("")
    head = gate.snapshot(tmp_path, ROLES)
    assert DEFERRED in head.deferred
    lost = set(head_snapshot.objects["headlamp"]) - set(head.objects["headlamp"])
    assert {(i.split("/")[0], i.split("/")[-1]) for i in lost} == {
        ("Role", "headlamp-prometheus-proxy"),
        ("RoleBinding", "headlamp-prometheus-proxy"),
    }
    problems = gate.removal_problems(head_snapshot, head, set())
    assert len(problems) == len(lost) and all(p.startswith("arm 3:") for p in problems)


def test_a_dropped_deferred_template_removes_the_kustomization(
    tmp_path: Path, head_snapshot
):
    gate.extract("HEAD", tmp_path, gate.RENDER_TREES)
    (tmp_path / ROLE / "templates" / TEMPLATE).unlink()
    defaults = tmp_path / ROLE / "defaults/main.yml"
    defaults.write_text(defaults.read_text() + "manifests_deferred_files: []\n")
    assert DEFERRED in head_snapshot.deferred
    assert DEFERRED not in gate.snapshot(tmp_path, ROLES).deferred
