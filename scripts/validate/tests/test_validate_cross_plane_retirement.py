"""Step 1 of *Retiring a flux-owned service* in docs/flux-deploy-plane.md, rendered (#5282).

bento-pdf stands in for the retiring service: it is flux-owned, ships one Deployment template,
and takes its Service and IngressRoute from the shared defaults, as texbrain does. Each test
renders a copy of HEAD's tree with its templates deleted.

Run: uv run pytest scripts/validate/tests/test_validate_cross_plane_retirement.py
"""

from pathlib import Path

import pytest

from validate import cross_plane_order as gate

# authelia reads `containers_list`, so it moves if the first PR edits the entry.
ROLES = ("bento-pdf", "authelia")
ROLE = "ansible/roles/k8s/bento-pdf"
# The include edits the doc's step 1 states, appended to the role's `k8s/manifests` include.
INCLUDE_VARS = (
    "  vars:\n"
    "    manifests_rollout: ''\n"
    "    manifests_exclude_files: [service.yaml, ingressroute.yaml]\n"
)
PATHS = [f"{ROLE}/templates/deployment.yaml.j2", f"{ROLE}/tasks/main.yml"]


@pytest.fixture(scope="module")
def head_snapshot(tmp_path_factory) -> gate.Snapshot:
    tree = tmp_path_factory.mktemp("head")
    gate.extract("HEAD", tree, ("ansible", "scripts"))
    return gate.snapshot(tree, ROLES)


def retired_step_1(tmp_path: Path, include_vars: str) -> gate.Snapshot:
    gate.extract("HEAD", tmp_path, ("ansible", "scripts"))
    for template in (tmp_path / ROLE / "templates").iterdir():
        template.unlink()
    tasks = tmp_path / ROLE / "tasks/main.yml"
    tasks.write_text(tasks.read_text() + include_vars)
    return gate.snapshot(tmp_path, ROLES)


def test_step_1_of_a_flux_owned_retirement_renders_no_object(tmp_path, head_snapshot):
    head = retired_step_1(tmp_path, INCLUDE_VARS)
    assert head_snapshot.objects["bento-pdf"]
    assert "bento-pdf" in head.renders
    assert not head.objects.get("bento-pdf")
    assert gate.counted(head_snapshot, head, PATHS, {}) == {"bento-pdf"}
    assert gate.problems(head_snapshot, head, PATHS, {}) == []
    declared = set(head_snapshot.objects["bento-pdf"])
    assert gate.removal_problems(head_snapshot, head, declared) == []


def test_step_1_without_the_include_edits_is_refused(tmp_path):
    with pytest.raises(gate.RenderFailed, match="restart target bento-pdf is no"):
        retired_step_1(tmp_path, "")
