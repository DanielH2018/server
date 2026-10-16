"""The kind node image CI's type proof boots runs the Kubernetes minor the cluster runs.

CI's `sops_manifest_type_proof` job (target slice D3, PR D3.3d) measures what kustomize-controller
and the API server do with an all-digit Secret value, in a kind cluster. The answer is only the
cluster's answer while the kind node's API server is on `k3s_version`'s minor, in
`ansible/roles/setup/k3s/defaults/main.yml`. Renovate offers the node image patch and digest
bumps only (issue #5173), so a k3s minor bump is what fails here, and the person finishing it
moves `KIND_NODE_IMAGE` to a tag the pinned kind release lists.

Run: uv run pytest ansible/tests/repo/test_kind_node_image_follows_k3s.py
"""

import re

from lib import yaml_fast
from _helpers import REPO

CI_REL = ".github/workflows/ci.yml"
K3S_DEFAULTS_REL = "ansible/roles/setup/k3s/defaults/main.yml"
STEP = "Create the kind cluster, the registry and the pinned Flux controllers"

_MINOR = re.compile(r"^v?(\d+)\.(\d+)\.")


def node_image(workflow: dict) -> str | None:
    """`KIND_NODE_IMAGE` from the proof job's cluster step, or None when the step is gone."""
    for step in workflow["jobs"]["sops_manifest_type_proof"]["steps"]:
        if step.get("name") == STEP:
            return (step.get("env") or {}).get("KIND_NODE_IMAGE")
    return None


def minor(version: str) -> tuple[int, int] | None:
    m = _MINOR.match(version)
    return (int(m[1]), int(m[2])) if m else None


def minor_problems(image: str | None, k3s_version: str) -> list[str]:
    if image is None:
        return [f"{CI_REL}'s sops_manifest_type_proof job has no `{STEP}` step"]
    tag = image.split("@")[0].rpartition(":")[2]
    if minor(tag) is None or minor(tag) != minor(k3s_version):
        return [f"the kind node image is {tag}, k3s_version is {k3s_version}"]
    return []


def test_a_node_image_on_the_k3s_minor_is_clean():
    assert minor_problems("kindest/node:v1.37.0@sha256:ab", "v1.37.1+k3s1") == []


def test_a_node_image_on_another_minor_is_flagged():
    assert minor_problems("kindest/node:v1.37.0@sha256:ab", "v1.38.0+k3s1") == [
        "the kind node image is v1.37.0, k3s_version is v1.38.0+k3s1"
    ]


def test_a_missing_step_is_flagged():
    assert minor_problems(None, "v1.37.1+k3s1")


def test_the_proof_boots_the_k3s_minor():
    workflow = yaml_fast.safe_load((REPO / CI_REL).read_text())
    defaults = yaml_fast.safe_load((REPO / K3S_DEFAULTS_REL).read_text())
    problems = minor_problems(node_image(workflow), str(defaults["k3s_version"]))
    assert problems == [], (
        f"{problems}. Move KIND_NODE_IMAGE in {CI_REL} to a kindest/node tag on k3s_version's "
        f"minor; the pinned kind release's notes list the images it built, with digests"
    )
