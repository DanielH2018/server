"""Whether `land.sh`'s `needs-manual-apply` note narrows the tag, and when it must not.

The deferral for an unapplyable setup role is quoted by four surfaces: the deployer's journal
line, its Discord alert, the SessionStart banner and this note. The deployer records the
narrow tags in its `manual_plane_tags` sidecar, one row per role spanning every range that
made the role pending (#2307). The note ends in the command that clears the role's line, so
the row is what has to run.

Nothing ties that row to THIS PR, though (#2324 review, finding 1). Read before the tick has
recorded this PR's range, it is absent or belongs to an earlier range. A stale `coredns` row
printed for an RBAC PR sends the operator to apply `coredns` and clear the marker, and the
RBAC change is never applied. So the note quotes a row only where it contains this PR's own
derivation, and re-reads it only after the awaited tick.

Run: uv run pytest scripts/deploy_tools/tests/test_land_quotes_the_deployers_narrowing.py
"""

import dataclasses
import json

import pytest

import land_tags
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


def confirmed(pr, sidecar):
    tree, rng = pr
    return land_tags.confirmed_narrow_tags([RBAC], rng, tree.root, sidecar)


# ── the row is quoted only where it covers this PR's own change ────────────────────────


def test_a_row_containing_this_prs_tags_is_quoted_whole(pr):
    """The row spans every pending range, and the clear command clears all of them."""
    narrow = confirmed(pr, "k3s coredns,kubeconfig")
    assert narrow == {"k3s": frozenset({"coredns", "kubeconfig"})}
    note = land_tags.plane_note([RBAC], narrow_tags=narrow)
    assert "ansible/k3s-bringup.yml --tags coredns,kubeconfig" in note


def test_a_stale_row_from_an_earlier_range_is_flagged(pr):
    """The rejecting half: `coredns` does not cover an RBAC edit, so the role tag prints."""
    assert confirmed(pr, "k3s coredns") == {}
    note = land_tags.plane_note([RBAC], narrow_tags={})
    assert "ansible/k3s-bringup.yml --tags k3s`" in note


def test_a_narrowed_note_clears_only_the_tags_it_told_you_to_apply(pr):
    """The clear beside a narrowed apply names `--applied` (#2349).

    A second PR touching the same role can widen the row between this note being printed and
    the operator running the clear. The bare form would drop that PR's tag too, leaving its
    change merged, unapplied and recorded nowhere.
    """
    narrow = confirmed(pr, "k3s kubeconfig")
    note = land_tags.plane_note([RBAC], narrow_tags=narrow)
    assert "clear-manual-plane k3s --applied kubeconfig" in note


def test_a_role_tag_note_clears_the_whole_line(pr):
    """The rejecting half: a whole-role apply covers whatever the row has gained."""
    note = land_tags.plane_note([RBAC], narrow_tags={})
    assert "clear-manual-plane k3s`" in note
    assert "--applied" not in note


@pytest.mark.parametrize(
    "sidecar", [None, "", "k3s -"], ids=["unreadable", "absent", "refused"]
)
def test_no_row_or_a_refused_row_is_flagged(pr, sidecar):
    assert confirmed(pr, sidecar) == {}


def test_no_pr_range_is_flagged(pr):
    tree, _ = pr
    assert (
        land_tags.confirmed_narrow_tags([RBAC], "", tree.root, "k3s kubeconfig") == {}
    )


def _plane_cli(pr, capsys, sidecar: str) -> str:
    tree, rng = pr
    payload = json.dumps({"files": [{"path": RBAC}], "changedFiles": 1})
    argv = ["--json", payload, "--plane", "--range", rng]
    assert land_tags.main(argv, sidecar=lambda: sidecar, repo=tree.root) == 0
    return capsys.readouterr().out


def test_the_plane_cli_quotes_a_covering_row(pr, capsys):
    assert "--tags kubeconfig`" in _plane_cli(pr, capsys, "k3s kubeconfig")


def test_the_plane_cli_does_not_print_a_stale_row(pr, capsys):
    """`land_tags.py --plane` read the sidecar and printed it with no check at all."""
    out = _plane_cli(pr, capsys, "k3s coredns")
    assert "--tags k3s`" in out
    assert "--tags coredns" not in out


# ── the landing re-reads the narrowing only after the tick it awaited ──────────────────

_RBAC_PR = {"files": [{"path": RBAC}], "changedFiles": 1}


def _landing(land_run, narrowing):
    f = Fakes(
        gh_views={"files,changedFiles": _RBAC_PR},
        derived=([], "pr"),
        state={"manual_plane_tags": "k3s kubeconfig"},
        narrowing=narrowing,
    )
    classifier = dataclasses.replace(
        build_classifier(f), plane_note=land_tags.plane_note
    )
    return land_run([], f, classifier=classifier)


def test_a_confirmed_narrowing_reaches_the_verdict_after_the_tick(land_run):
    _rc, out, _err, calls, _ = _landing(land_run, {"k3s": frozenset({"kubeconfig"})})
    names = [c[0] for c in calls]
    assert names.index("tick") < names.index("confirm_narrowing"), (
        "the narrowing is read after the tick records this PR's range, never before"
    )
    assert "ansible/k3s-bringup.yml --tags kubeconfig" in out
    assert "--tags k3s`" not in out


def test_an_unconfirmed_narrowing_leaves_the_role_tag_in_the_verdict(land_run):
    _rc, out, _err, _calls, _ = _landing(land_run, {})
    assert "ansible/k3s-bringup.yml --tags k3s`" in out


def test_a_plane_note_that_raises_after_the_tick_keeps_the_role_tag(land_run):
    """A raise in the re-render ends in the step 1 note, not in a traceback (#2350).

    `plane_note` already ran on these inputs in step 1, so this is unlikely — which is exactly
    why it was outside the `try`. `land.py` returning a traceback instead of a VERDICT costs
    the operator the whole landing; the role tag costs them a wider apply.
    """
    calls_made = []

    def plane_note(paths, declared=None, *, quiet=False, narrow_tags=None):
        calls_made.append(narrow_tags)
        if narrow_tags:
            raise RuntimeError("the re-render blew up")
        return land_tags.plane_note(paths, declared, quiet=quiet)

    f = Fakes(
        gh_views={"files,changedFiles": _RBAC_PR},
        derived=([], "pr"),
        state={"manual_plane_tags": "k3s kubeconfig"},
        narrowing={"k3s": frozenset({"kubeconfig"})},
    )
    classifier = dataclasses.replace(build_classifier(f), plane_note=plane_note)
    _rc, out, _err, _calls, _ = land_run([], f, classifier=classifier)
    assert calls_made[-1] == {"k3s": frozenset({"kubeconfig"})}, (
        "the re-render was tried"
    )
    assert "VERDICT" in out
    assert "ansible/k3s-bringup.yml --tags k3s`" in out
