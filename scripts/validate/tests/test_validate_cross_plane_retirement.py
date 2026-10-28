"""Step 1 of *Retiring a flux-owned service* in docs/flux-deploy-plane.md, rendered (#5282).

bento-pdf stands in for the retiring service: it is flux-owned, ships one Deployment template,
and takes its Service and IngressRoute from the shared defaults, as texbrain does. Each test
renders a copy of HEAD's tree with its templates deleted and the include edits the doc's
yaml block states, read from the doc itself.

Run: uv run pytest scripts/validate/tests/test_validate_cross_plane_retirement.py
"""

import re
import textwrap
from pathlib import Path

import pytest

from validate import render_diff_gates as gate

# authelia reads `containers_list`, so it moves if the first PR edits the entry.
ROLES = ("bento-pdf", "authelia")
ROLE = "ansible/roles/k8s/bento-pdf"
DOC = gate.REPO / "docs/flux-deploy-plane.md"
SECTION = "## Retiring a flux-owned service\n"


def documented_include_vars(text: str) -> str:
    """The first fenced yaml block of the doc's retirement section, indented under `- name:`.

    The doc is the oracle: the test renders whatever step 1 tells the PR to add.
    """
    section = text.split(SECTION, 1)[1].split("\n## ", 1)[0]
    match = re.search(r"```yaml\n(.*?)\n *```", section, re.DOTALL)
    assert match, f"{DOC.name} {SECTION.strip()} holds no yaml block"
    return textwrap.indent(textwrap.dedent(match.group(1)), "  ") + "\n"


@pytest.fixture(scope="module")
def head_snapshot(tmp_path_factory) -> gate.Snapshot:
    tree = tmp_path_factory.mktemp("head")
    gate.extract("HEAD", tree, ("ansible", "scripts", "hosts"))
    return gate.snapshot(tree, ROLES)


def retired_step_1(tmp_path: Path, include_vars: str) -> gate.Snapshot:
    gate.extract("HEAD", tmp_path, ("ansible", "scripts", "hosts"))
    for template in (tmp_path / ROLE / "templates").iterdir():
        template.unlink()
    tasks = tmp_path / ROLE / "tasks/main.yml"
    tasks.write_text(tasks.read_text() + include_vars)
    return gate.snapshot(tmp_path, ROLES)


def test_step_1_of_a_flux_owned_retirement_renders_no_object(tmp_path, head_snapshot):
    head = retired_step_1(tmp_path, documented_include_vars(DOC.read_text()))
    assert head_snapshot.objects["bento-pdf"]
    assert "bento-pdf" in head.renders
    assert not head.objects.get("bento-pdf")
    assert gate.counted(head_snapshot, head) == {"bento-pdf"}
    assert gate.problems(head_snapshot, head) == []
    declared = set(head_snapshot.objects["bento-pdf"])
    assert gate.removal_problems(head_snapshot, head, declared) == []


def test_step_1_without_the_include_edits_is_refused(tmp_path):
    with pytest.raises(gate.RenderFailed, match="restart target bento-pdf is no"):
        retired_step_1(tmp_path, "")
