"""The cross-plane gate's `_is_clean` / `_is_flagged` pairs, one per bullet of D4.4's step.

Most pairs judge two synthetic snapshots, with the dependency map built by the play's own
`build_k8s_dep_map`. The `hostname`/`auth_tier` pair and the `workload-shell.yml.j2` pair only
prove something through a real render, because the question is which roles' output an edit
moves; they render bento-pdf and authelia from a copy of HEAD's tree.

Run: uv run pytest scripts/validate/tests/test_validate_cross_plane_order.py
"""

import re
import subprocess
from pathlib import Path

import pytest

from deploy_tools.object_loss import PRUNE_KEY, prune_disabled
from deploy_tools.render_targets import render_targets
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


def judge(moved, base_kw=None, head_kw=None, paths=(), diffs=None):
    base = snap(moved, side="base", **(base_kw or {}))
    head = snap(moved, side="head", **(head_kw or {}))
    return gate.problems(base, head, paths, diffs or {})


def test_a_change_to_bento_pdf_alone_is_clean():
    assert judge({"bento-pdf"}) == []


def test_a_template_change_in_bento_pdf_and_authelia_together_is_flagged():
    flagged = judge({"bento-pdf", "authelia"})
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


# What a real renderer PR carries beside the renderer: docs, tests and the shard weights. None
# of them reaches an Ansible apply.
RENDERER_WITH_DOCS_AND_TESTS = RENDERER_ONLY + [
    "docs/homelab-render.md",
    "scripts/renderer/tests/test_renderer_cluster.py",
    "ansible/tests/repo/test_census_rows_text.py",
    "scripts/dev/pytest_shard_weights.json",
]


def test_a_renderer_change_with_its_docs_and_tests_is_clean():
    assert judge({"bento-pdf", "authelia"}, paths=RENDERER_WITH_DOCS_AND_TESTS) == []


@pytest.mark.parametrize(
    "beside",
    [
        "ansible/templates/workload-shell.yml.j2",
        "ansible/roles/k8s/authelia/templates/configmap.yaml.j2",
        "scripts/lib/plain_scalar.py",
        "scripts/diagnostics/probe_lib/obs_api.py",
        "uv.lock",
        "ansible.cfg",
    ],
)
def test_a_renderer_change_beside_a_path_ansible_reads_is_flagged(beside):
    flagged = judge({"bento-pdf", "authelia"}, paths=RENDERER_ONLY + [beside])
    assert len(flagged) == 1 and flagged[0].startswith("arm 1: bento-pdf (flux)")


def test_ansible_scripts_names_the_files_a_task_ships():
    """homelab-mcp's tasks ship obs_api.py into its image build."""
    assert "scripts/diagnostics/probe_lib/obs_api.py" in gate.ansible_scripts()


def test_a_change_beside_netpol_baseline_is_flagged_through_applies_last():
    flagged = judge({"bento-pdf", "netpol-baseline"})
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
    assert gate.counted(head_snapshot, head, [], {}) == {"bento-pdf", "authelia"}
    flagged = gate.problems(head_snapshot, head, [str(gate.HOST_VARS)], {})
    assert len(flagged) == 1 and flagged[0].startswith("arm 1: bento-pdf (flux)")


def test_an_auth_tier_edit_that_moves_authelia_alone_is_clean(tmp_path, head_snapshot):
    edit = _bento_entry_edit("auth_tier", "one_factor", "two_factor")
    head = edited(tmp_path, {str(gate.HOST_VARS): edit})
    assert gate.counted(head_snapshot, head, [], {}) == {"authelia"}
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


# Arms 3 and 4 (D5.1f), the removal gates. bento-pdf is flux-owned in the fixtures.
NETPOL = "NetworkPolicy/homelab/bento-pdf"
CLUSTER_ROLE = "ClusterRole//bento-pdf-reader"
BENTO = {"Deployment/homelab/bento-pdf", NETPOL, CLUSTER_ROLE}


def removal(base_objects, head_objects, declared=(), head_flux=("bento-pdf",), **kw):
    base = gate.Snapshot(
        renders={},
        owners={"bento-pdf": "flux"},
        deps={},
        objects={"bento-pdf": base_objects},
        unpruned=kw.get("unpruned", ()),
        retired=kw.get("base_retired", ()),
    )
    head = gate.Snapshot(
        renders={},
        owners={t: "flux" for t in head_flux},
        deps={},
        objects={"bento-pdf": head_objects},
        retired=kw.get("head_retired", ()),
    )
    return gate.removal_problems(base, head, declared)


@pytest.mark.parametrize("dropped", [NETPOL, CLUSTER_ROLE])
def test_a_dropped_object_without_a_trailer_is_flagged(dropped):
    flagged = removal(BENTO, BENTO - {dropped})
    assert len(flagged) == 1
    assert flagged[0].startswith(f"arm 3: bento-pdf no longer renders {dropped}")
    assert f"`Removes-Object: {dropped}`" in flagged[0]


@pytest.mark.parametrize("dropped", [NETPOL, CLUSTER_ROLE])
def test_a_dropped_object_with_its_trailer_is_clean(dropped):
    messages = f"Drop the reader\n\nRemoves-Object: {dropped}\n"
    declared = gate.removes_object.declared(messages)
    assert removal(BENTO, BENTO - {dropped}, declared) == []


def test_a_trailer_for_another_object_leaves_the_drop_flagged():
    assert len(removal(BENTO, BENTO - {NETPOL}, {CLUSTER_ROLE})) == 1


def test_a_snapshot_job_renamed_by_its_digest_is_clean():
    base = BENTO | {"Job/homelab/bento-pdf-snapshot-0a1b2c3d"}
    head = BENTO | {"Job/homelab/bento-pdf-snapshot-9f8e7d6c"}
    assert removal(base, head) == []


def test_a_digest_rename_to_another_kind_is_flagged():
    base = BENTO | {"Job/homelab/bento-pdf-snapshot-0a1b2c3d"}
    head = BENTO | {"ConfigMap/homelab/bento-pdf-snapshot-9f8e7d6c"}
    assert len(removal(base, head)) == 1


def test_a_dropped_prune_disabled_claim_is_clean():
    claim = "PersistentVolumeClaim/homelab/bento-pdf-data"
    assert removal(BENTO | {claim}, BENTO, unpruned={claim}) == []


def test_the_revert_of_a_flip_is_clean():
    """A handback drops the tag's whole Flux render, which Orphan deletes nothing of."""
    assert removal(BENTO, set(), head_flux=()) == []


def test_a_flux_owned_tag_that_renders_nothing_is_flagged():
    assert len(removal(BENTO, set())) == len(BENTO)


def test_an_object_moved_to_an_ansible_owned_tag_is_flagged():
    """Flux still prunes it from bento-pdf, and Ansible applies authelia only later."""
    base = gate.Snapshot(
        renders={},
        owners={"bento-pdf": "flux", "authelia": "ansible"},
        deps={},
        objects={"bento-pdf": BENTO},
    )
    head = gate.Snapshot(
        renders={},
        owners=base.owners,
        deps={},
        objects={"bento-pdf": BENTO - {NETPOL}, "authelia": {NETPOL}},
    )
    assert len(gate.removal_problems(base, head, ())) == 1


def test_an_object_moved_to_another_flux_owned_tag_is_clean():
    flux = {"bento-pdf": "flux", "texbrain": "flux"}
    base = gate.Snapshot(renders={}, owners=flux, deps={}, objects={"bento-pdf": BENTO})
    head = gate.Snapshot(
        renders={},
        owners=flux,
        deps={},
        objects={"bento-pdf": BENTO - {NETPOL}, "texbrain": {NETPOL}},
    )
    assert gate.removal_problems(base, head, ()) == []


def test_a_flux_owned_entry_moved_into_k8s_retired_services_is_flagged():
    flagged = removal(BENTO, BENTO, head_flux=(), head_retired={"bento-pdf"})
    assert len(flagged) == 1 and flagged[0].startswith("arm 4: bento-pdf joins")


def test_retiring_an_ansible_owned_entry_is_clean():
    base = gate.Snapshot(renders={}, owners={"authelia": "ansible"}, deps={})
    head = gate.Snapshot(renders={}, owners={}, deps={}, retired={"authelia"})
    assert gate.removal_problems(base, head, ()) == []


def test_a_name_already_retired_on_the_base_is_clean():
    retired = {"bento-pdf"}
    assert removal(BENTO, BENTO, base_retired=retired, head_retired=retired) == []


def test_the_snapshot_names_objects_as_decide_does(head_snapshot):
    """Arm 3's identities are the ones `promote.py decide` matches the trailer against."""
    assert "Deployment/homelab/bento-pdf" in head_snapshot.objects["bento-pdf"]
    listed = gate.yaml_fast.safe_load((gate.REPO / gate.GROUP_VARS).read_text())
    assert set(head_snapshot.retired) == {
        e["name"] for e in listed.get("k8s_retired_services") or []
    }


def test_a_prune_disabled_label_or_annotation_is_read():
    for key in ("labels", "annotations"):
        assert prune_disabled({"metadata": {key: {PRUNE_KEY: "disabled"}}})
    assert not prune_disabled({"metadata": {"labels": {PRUNE_KEY: "x"}}})


def _digests(own: str, suspended: bool) -> dict:
    """A `digests.json` whose bento-pdf entry carries `own` and `suspended`."""
    return {"bento-pdf": {"own": own, "footprint": own, "suspended": suspended}}


def suspended_snap(own: str, suspended: bool) -> gate.Snapshot:
    return gate.Snapshot(
        renders={},
        owners={"bento-pdf": "flux"},
        deps={},
        digests=_digests(own, suspended),
    )


def test_a_change_to_a_suspended_tags_render_is_flagged():
    flagged = gate.suspended_problems(
        suspended_snap("a", True), suspended_snap("b", True)
    )
    assert len(flagged) == 1 and flagged[0].startswith("arm 5: bento-pdf is suspended")


def test_the_same_change_with_the_suspend_removed_is_clean():
    assert (
        gate.suspended_problems(suspended_snap("a", True), suspended_snap("b", False))
        == []
    )


def test_suspending_a_tag_in_the_same_pr_as_a_change_is_clean():
    """The suspend PR itself: the tag is not suspended at the base."""
    assert (
        gate.suspended_problems(suspended_snap("a", False), suspended_snap("b", True))
        == []
    )


def test_the_gates_own_snapshot_reads_digests_json(head_tree):
    """With no roles, as `render_side` calls it, `snapshot` still passes every render target
    with `--role`, so the bundle's `digests.json` is there to read. A render without `--role`
    writes no bundle, and arm 5 would read no tag as suspended. The real target list is
    narrowed to two of its tags: the `--role` contract holds per tag, and every tag cost 13s."""

    def two_targets(*args):
        found = render_targets(*args)
        assert set(ROLES) <= set(found), found
        return list(ROLES)

    snapshot = gate.snapshot(head_tree, targets=two_targets)
    assert snapshot.digests["bento-pdf"]["suspended"] is False
    assert "authelia" in snapshot.renders


def _git(repo: Path, *args: str) -> str:
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "HOME": str(repo),
        "PATH": "/usr/bin:/bin",
    }
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


@pytest.mark.parametrize(
    "messages, expected",
    [
        (["Drop the policy"], gate.REFUSED),
        ([f"Drop the policy\n\nRemoves-Object: {NETPOL}", "Tidy"], gate.CLEAN),
    ],
)
def test_gate_reads_the_trailers_from_the_prs_commits(tmp_path, messages, expected):
    """`gate()` hands `removal_problems` the lines of every commit in `base..head`.

    The base commit carries the line too, and it is outside the range, so it declares nothing.
    """
    _git(tmp_path, "init", "-q")
    _git(
        tmp_path,
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        f"Base\n\nRemoves-Object: {NETPOL}",
    )
    base = _git(tmp_path, "rev-parse", "HEAD")
    for message in messages:
        _git(tmp_path, "commit", "-q", "--allow-empty", "-m", message)
    snaps = {
        "base": gate.Snapshot(
            renders={},
            owners={"bento-pdf": "flux"},
            deps={},
            objects={"bento-pdf": BENTO},
        ),
        "head": gate.Snapshot(
            renders={},
            owners={"bento-pdf": "flux"},
            deps={},
            objects={"bento-pdf": BENTO - {NETPOL}},
        ),
    }
    render = lambda sha, dest, repo: snaps[dest.name]
    assert gate.gate(base, "HEAD", repo=tmp_path, render=render) == expected
