"""The cross-plane gate's `_is_clean` / `_is_flagged` pairs, one per bullet of D4.4's step.

Most pairs judge two synthetic snapshots, with the dependency map built by the play's own
`build_k8s_dep_map`. The `hostname`/`auth_tier` pair and the `workload-shell.yml.j2` pair only
prove something through a real render, because the question is which roles' output an edit
moves; they render bento-pdf and authelia from a copy of HEAD's tree.

Run: uv run pytest scripts/validate/tests/test_validate_cross_plane_order.py
"""

import re
from pathlib import Path

import pytest

from validate import cross_plane_order as gate
from toposort import build_k8s_dep_map

AUTHELIA_IMAGE_BUMP = (
    "--- a/ansible/roles/k8s/authelia/defaults/main.yml\n"
    "+++ b/ansible/roles/k8s/authelia/defaults/main.yml\n"
    "-authelia_k8s_image: authelia/authelia:4.39.1\n"
    "+authelia_k8s_image: authelia/authelia:4.39.2\n"
)
BENTO_IMAGE_BUMP = AUTHELIA_IMAGE_BUMP.replace("authelia", "bento_pdf")


def snap(moved=(), flux=("bento-pdf",), texbrain_deps=(), side="base"):
    """A snapshot of four fixture entries; each tag in `moved` renders differently per side."""
    entries: list[dict] = [
        {"name": "authelia"},
        {"name": "bento-pdf", "use_authelia": True},
        {"name": "texbrain", "use_authelia": True, "depends_on": list(texbrain_deps)},
        {"name": "netpol-baseline"},
    ]
    names = [e["name"] for e in entries]
    return gate.Snapshot(
        renders={n: f"{n}-{side}" if n in moved else n for n in names},
        owners={n: "flux" if n in flux else "ansible" for n in names},
        deps=build_k8s_dep_map(entries, "/nonexistent"),
    )


# A template edit under `ansible/`, so an Ansible-owned tag's render move counts.
TEMPLATE_EDIT = ["ansible/roles/k8s/authelia/templates/configmap.yaml.j2"]


def judge(moved, base_kw=None, head_kw=None, paths=(), diffs=None):
    base = snap(moved, side="base", **(base_kw or {}))
    head = snap(moved, side="head", **(head_kw or {}))
    return gate.problems(base, head, paths, diffs or {})


def test_a_change_to_bento_pdf_alone_is_clean():
    assert judge({"bento-pdf"}) == []


def test_a_template_change_in_bento_pdf_and_authelia_together_is_flagged():
    flagged = judge({"bento-pdf", "authelia"}, paths=TEMPLATE_EDIT)
    assert len(flagged) == 1
    assert flagged[0].startswith(
        "arm 1: bento-pdf (flux) depends on authelia (ansible)"
    )


def test_an_image_pin_bump_in_both_roles_is_clean():
    diffs = {"bento-pdf": BENTO_IMAGE_BUMP, "authelia": AUTHELIA_IMAGE_BUMP}
    assert judge({"bento-pdf", "authelia"}, diffs=diffs) == []


def test_an_image_pin_bump_beside_a_shared_template_change_is_flagged():
    diffs = {"bento-pdf": BENTO_IMAGE_BUMP, "authelia": AUTHELIA_IMAGE_BUMP}
    paths = ["ansible/templates/workload-shell.yml.j2"]
    assert len(judge({"bento-pdf", "authelia"}, paths=paths, diffs=diffs)) == 1


def test_a_tasks_change_counts_though_it_moves_no_render_is_flagged():
    flagged = judge({"bento-pdf"}, paths=["ansible/roles/k8s/authelia/tasks/main.yml"])
    assert len(flagged) == 1


def test_a_tasks_change_in_a_flux_owned_role_is_clean():
    """bento-pdf's tasks run nowhere once Flux owns it, so they pair with nothing (OQ 9)."""
    assert (
        judge({"authelia"}, paths=["ansible/roles/k8s/bento-pdf/tasks/main.yml"]) == []
    )


# A renderer-only PR: nothing publishes an Ansible-owned tag's `homelab-render` output, so
# only the flux-owned half of what it moves reaches a cluster (#5213).
RENDERER_ONLY = ["scripts/renderer/renderer_lib/cluster.py"]


def test_a_renderer_only_change_moving_bento_pdf_and_authelia_is_clean():
    assert judge({"bento-pdf", "authelia"}, paths=RENDERER_ONLY) == []


def test_a_renderer_only_change_counts_the_flux_owned_tag_alone():
    base = snap({"bento-pdf", "authelia"}, side="base")
    head = snap({"bento-pdf", "authelia"}, side="head")
    assert gate.counted(base, head, RENDERER_ONLY, {}) == {"bento-pdf"}


def test_a_change_beside_netpol_baseline_is_flagged_through_applies_last():
    flagged = judge({"bento-pdf", "netpol-baseline"}, paths=TEMPLATE_EDIT)
    assert len(flagged) == 1
    assert "netpol-baseline (ansible) depends on bento-pdf (flux)" in flagged[0]


def test_an_edge_added_with_a_change_to_the_dependent_is_flagged_by_arm_2():
    flux = ("bento-pdf", "texbrain")
    flagged = judge(
        {"texbrain"},
        base_kw={"flux": flux},
        head_kw={"flux": flux, "texbrain_deps": ["bento-pdf"]},
    )
    assert flagged == [
        "arm 2: texbrain gains dependsOn ['bento-pdf'] and its own render moves in the same "
        "PR. Land the edge alone first, then the change."
    ]


def test_the_edge_alone_is_clean():
    flux = ("bento-pdf", "texbrain")
    head_kw = {"flux": flux, "texbrain_deps": ["bento-pdf"]}
    assert judge(set(), base_kw={"flux": flux}, head_kw=head_kw) == []


def test_a_flip_that_moves_the_render_and_gains_edges_is_clean():
    head_kw = {"flux": ("bento-pdf", "texbrain"), "texbrain_deps": ["bento-pdf"]}
    base, head = snap({"texbrain"}), snap({"texbrain"}, side="head", **head_kw)
    assert head.depends_on("texbrain") - base.depends_on("texbrain") == {"bento-pdf"}
    assert gate.problems(base, head, [], {}) == []


# The real-render pairs. One HEAD copy per test, rendered for the two roles the edits reach.
ROLES = ("bento-pdf", "authelia")
SHELL = "ansible/templates/workload-shell.yml.j2"
# The macro body's line, not the header comment's mention of the same field.
SHELL_EDIT = (
    "  revisionHistoryLimit: 3\n  strategy:",
    "  revisionHistoryLimit: 5\n  strategy:",
)


@pytest.fixture(scope="module")
def head_tree(tmp_path_factory) -> Path:
    tree = tmp_path_factory.mktemp("head")
    gate.extract("HEAD", tree, ("ansible", "scripts"))
    return tree


@pytest.fixture(scope="module")
def head_snapshot(head_tree) -> gate.Snapshot:
    return gate.snapshot(head_tree, ROLES)


def edited(tmp_path: Path, edits: dict[str, tuple[str, str]]) -> gate.Snapshot:
    """HEAD's tree with each `path: (old, new)` replacement made once, rendered."""
    gate.extract("HEAD", tmp_path, ("ansible", "scripts"))
    for path, (old, new) in edits.items():
        text = (tmp_path / path).read_text()
        assert text.count(old) >= 1, f"{path} no longer holds {old!r}"
        (tmp_path / path).write_text(text.replace(old, new, 1))
    return gate.snapshot(tmp_path, ROLES)


def _bento_entry_edit(field: str, value: str, new: str) -> tuple[str, str]:
    text = (gate.REPO / gate.HOST_VARS).read_text()
    match = re.search(r"  - name: bento-pdf\n(?:    .*\n|\s*#.*\n)+", text)
    assert match, "the inventory has no bento-pdf entry"
    entry = match.group(0)
    assert f"{field}: {value}" in entry
    return entry, entry.replace(f"{field}: {value}", f"{field}: {new}")


def test_an_inventory_edit_that_moves_bento_pdf_and_authelia_is_flagged(
    tmp_path, head_snapshot
):
    edit = _bento_entry_edit("hostname", '"bento-pdf"', '"pdf"')
    head = edited(tmp_path, {str(gate.HOST_VARS): edit})
    assert gate.counted(head_snapshot, head, [str(gate.HOST_VARS)], {}) == {
        "bento-pdf",
        "authelia",
    }
    flagged = gate.problems(head_snapshot, head, [str(gate.HOST_VARS)], {})
    assert len(flagged) == 1 and flagged[0].startswith("arm 1: bento-pdf (flux)")


def test_an_auth_tier_edit_that_moves_authelia_alone_is_clean(tmp_path, head_snapshot):
    edit = _bento_entry_edit("auth_tier", "one_factor", "two_factor")
    head = edited(tmp_path, {str(gate.HOST_VARS): edit})
    assert gate.counted(head_snapshot, head, [str(gate.HOST_VARS)], {}) == {"authelia"}
    assert gate.problems(head_snapshot, head, [str(gate.HOST_VARS)], {}) == []


def test_a_shared_template_change_reaching_both_planes_is_flagged(
    tmp_path, head_snapshot
):
    head = edited(tmp_path, {SHELL: SHELL_EDIT})
    assert gate.counted(head_snapshot, head, [SHELL], {}) == {"bento-pdf", "authelia"}
    assert gate.problems(head_snapshot, head, [SHELL], {})


def test_step_1_of_copy_and_migrate_is_clean(tmp_path, head_tree, head_snapshot):
    """The changed macro under a new name, with only the Ansible-owned importer moved to it."""
    copy = "ansible/templates/workload-shell-next.yml.j2"
    text = (head_tree / SHELL).read_text()
    (tmp_path / "ansible/templates").mkdir(parents=True)
    (tmp_path / copy).write_text(text.replace(*SHELL_EDIT))
    imports = ("'workload-shell.yml.j2'", "'workload-shell-next.yml.j2'")
    head = edited(
        tmp_path,
        {
            "ansible/roles/k8s/authelia/templates/deployment.yaml.j2": imports,
            "ansible/roles/k8s/authelia/templates/deployment-redis.yaml.j2": imports,
        },
    )
    assert gate.counted(head_snapshot, head, [copy], {}) == {"authelia"}
    assert gate.problems(head_snapshot, head, [copy], {}) == []
