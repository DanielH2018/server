"""Whether `land.sh`'s `needs-manual-apply` note narrows the tag, and when it must not.

The deferral for an unapplyable setup role ends in a command that applies the role and clears
its `manual_plane` line. The landing quotes the narrowing derived from the PR's own range,
and keeps the whole-role tag when that derivation refuses.

Run: uv run pytest scripts/deploy_tools/tests/test_land_narrows_the_plane_note.py
"""

import dataclasses
import json

import pytest

from deploy_tools.land_lib import land_tags, land_tags_cli
from _land_fakes import Fakes, build_classifier
from _narrow_fixtures import Tree, _refs

RBAC = "ansible/roles/setup/k3s/templates/readonly-rbac.yaml.j2"
ROLE = "ansible/roles/setup/k3s"


@pytest.fixture
def pr(tmp_path) -> tuple[Tree, str]:
    """A checkout holding a two-topic k3s role, and the range of a PR editing its RBAC."""
    tree = Tree(tmp_path / "repo")
    tree.write(
        "ansible/k3s-bringup.yml",
        "---\n- name: Bring up\n  hosts: localhost\n  roles:\n"
        '    - { role: k3s, tags: ["k3s"] }\n',
    )
    tree.write(
        f"{ROLE}/tasks/main.yml",
        "---\n- name: Kubeconfig\n  ansible.builtin.import_tasks: kubeconfig.yml\n"
        "- name: CoreDNS\n  ansible.builtin.import_tasks: coredns.yml\n",
    )
    tree.write(
        f"{ROLE}/tasks/kubeconfig.yml",
        "---\n- name: Apply the RBAC\n  ansible.builtin.template:\n"
        "    src: readonly-rbac.yaml.j2\n    dest: /tmp/rbac.yaml\n  tags: [kubeconfig]\n",
    )
    tree.write(
        f"{ROLE}/tasks/coredns.yml",
        "---\n- name: Point DNS\n  ansible.builtin.debug: {}\n  tags: [coredns]\n",
    )
    tree.write(f"{ROLE}/templates/readonly-rbac.yaml.j2", "kind: ClusterRole\n")
    tree.commit("base")
    tree.write(f"{ROLE}/templates/readonly-rbac.yaml.j2", "kind: ClusterRole # more\n")
    old, new = _refs(tree)
    return tree, f"{old}..{new}"


# ── the note a narrowing renders ─────────────────────────────────────────────────────


def test_a_narrowed_note_clears_only_the_tags_it_told_you_to_apply(pr):
    """The clear beside a narrowed apply names `--applied`.

    A second PR touching the same role can widen the `manual_plane_tags` row between this note
    being printed and the operator running the clear. The bare form would drop that PR's tag
    too, leaving its change merged, unapplied and recorded nowhere.
    """
    note = land_tags.plane_note([RBAC], narrow_tags={"k3s": frozenset({"kubeconfig"})})
    assert "ansible/k3s-bringup.yml --tags kubeconfig" in note
    assert "clear-owed manual_plane k3s --applied kubeconfig" in note


def test_a_role_tag_note_clears_the_whole_line(pr):
    """The rejecting half: a whole-role apply covers whatever the row has gained."""
    note = land_tags.plane_note([RBAC], narrow_tags={})
    assert "ansible/k3s-bringup.yml --tags k3s`" in note
    assert "clear-owed manual_plane k3s`" in note
    assert "--applied" not in note


def test_the_plane_cli_narrows_from_this_prs_own_range(pr, capsys):
    """The CLI has no merge commit to find a receipt by, so it derives the PR's own tags."""
    tree, rng = pr
    payload = json.dumps({"files": [{"path": RBAC}], "changedFiles": 1})
    argv = ["--json", payload, "--plane", "--range", rng]
    assert land_tags_cli.main(argv, repo=tree.root) == 0
    assert "--tags kubeconfig`" in capsys.readouterr().out


def test_the_plane_cli_names_a_changed_bringup_playbook_over_a_range(tmp_path, capsys):
    """A playbook-only PR adds no setup role, and with `--range` the note named the
    `initial_setup.yml --tags <role>` placeholder, which matches no tag (#4282)."""
    tree = Tree(tmp_path / "repo")
    tree.write("ansible/k3s-bringup.yml", "---\n- name: Bring up\n  hosts: localhost\n")
    tree.commit("base")
    tree.write("ansible/k3s-bringup.yml", "---\n- name: Bring up\n  hosts: all\n")
    old, new = _refs(tree)
    payload = json.dumps(
        {"files": [{"path": "ansible/k3s-bringup.yml"}], "changedFiles": 1}
    )
    argv = ["--json", payload, "--plane", "--range", f"{old}..{new}"]
    assert land_tags_cli.main(argv, repo=tree.root) == 0
    out = capsys.readouterr().out
    assert "`uv run ansible-playbook ansible/k3s-bringup.yml --tags k3s`" in out
    assert "<role>" not in out


# ── a landing that deploys its own merge commit narrows from its own range ──────


def test_own_narrowing_is_this_prs_derivation_with_no_row(pr):
    tree, rng = pr
    assert land_tags.own_narrow_tags([RBAC], rng, tree.root) == {
        "k3s": frozenset({"kubeconfig"})
    }


def test_own_narrowing_with_no_range_is_flagged(pr):
    tree, _ = pr
    assert land_tags.own_narrow_tags([RBAC], "", tree.root) == {}


def test_own_narrowing_answers_nothing_for_a_range_no_task_reads(pr):
    """A defaults edit that changes no key reaches no host through k3s (#4408).

    The derivation's `NothingToApply` subclasses its refusal, and before this the generic
    arm read it as one, so the note kept `--tags k3s` for a change nobody has to apply.
    """
    tree, _ = pr
    defaults = f"{ROLE}/defaults/main.yml"
    tree.write(defaults, "---\nk3s_channel: stable\n")
    tree.commit("defaults")
    tree.write(defaults, "---\n# The upstream channel.\nk3s_channel: stable\n")
    old, new = _refs(tree)
    assert land_tags.own_narrow_tags([defaults], f"{old}..{new}", tree.root) == {
        "k3s": frozenset()
    }


_MIXED_PR = {
    "files": [{"path": RBAC}, {"path": "ansible/roles/k8s/obs/x.j2"}],
    "changedFiles": 2,
}


def _fast_landing(land_run, own):
    """A service tag AND a k3s template, so the tick is never awaited."""
    f = Fakes(
        gh_views={"files,changedFiles": _MIXED_PR},
        derived=(["obs"], "pr"),
        own_narrowing=own,
    )
    classifier = dataclasses.replace(
        build_classifier(f), plane_note=land_tags.plane_note
    )
    return land_run([], f, classifier=classifier)


def test_the_fast_path_prints_this_prs_narrow_tag(land_run):
    _rc, out, _err, calls, _ = _fast_landing(
        land_run, {"k3s": frozenset({"kubeconfig"})}
    )
    assert "own_narrowing" in [c[0] for c in calls]
    assert "ansible/k3s-bringup.yml --tags kubeconfig" in out
    assert "clear-owed manual_plane k3s --applied kubeconfig" in out
    assert "--tags k3s`" not in out


def test_the_fast_path_keeps_the_role_tag_when_its_derivation_refuses(land_run):
    _rc, out, _err, _calls, _ = _fast_landing(land_run, {})
    assert "ansible/k3s-bringup.yml --tags k3s`" in out


def test_the_fast_path_leaves_out_a_role_the_range_needs_nothing_from(land_run):
    """The tick writes no `manual_plane` line for it, so neither apply nor clear is owed."""
    _rc, out, _err, _calls, _ = _fast_landing(land_run, {"k3s": frozenset()})
    assert "k3s-bringup.yml" not in out
    assert "clear-owed manual_plane k3s" not in out
