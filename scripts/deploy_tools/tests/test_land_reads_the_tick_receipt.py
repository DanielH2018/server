"""Whether `land.sh`'s `needs-manual-apply` note narrows the tag, and when it must not.

The deferral for an unapplyable setup role ends in a command that applies the role and clears
its `manual_plane` line. After the tick it awaited, the landing reads the deployer's receipt
for the tick that crossed its merge commit (#3391) and quotes that tick's narrowing of the
role. The receipt is scoped to one range containing this PR, so nothing has to be re-derived
to tie it to the PR — which is what the `manual_plane_tags` sidecar needed, since its row
spans every range that made the role pending. No receipt, a receipt for a range that does not
contain the PR, and a role the receipt could not narrow all keep the whole-role tag.

Run: uv run pytest scripts/deploy_tools/tests/test_land_reads_the_tick_receipt.py
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


# ── the note a narrowing renders ─────────────────────────────────────────────────────


def test_a_narrowed_note_clears_only_the_tags_it_told_you_to_apply(pr):
    """The clear beside a narrowed apply names `--applied`.

    A second PR touching the same role can widen the `manual_plane_tags` row between this note
    being printed and the operator running the clear. The bare form would drop that PR's tag
    too, leaving its change merged, unapplied and recorded nowhere.
    """
    note = land_tags.plane_note([RBAC], narrow_tags={"k3s": frozenset({"kubeconfig"})})
    assert "ansible/k3s-bringup.yml --tags kubeconfig" in note
    assert "clear-manual-plane k3s --applied kubeconfig" in note


def test_a_role_tag_note_clears_the_whole_line(pr):
    """The rejecting half: a whole-role apply covers whatever the row has gained."""
    note = land_tags.plane_note([RBAC], narrow_tags={})
    assert "ansible/k3s-bringup.yml --tags k3s`" in note
    assert "clear-manual-plane k3s`" in note
    assert "--applied" not in note


def test_the_plane_cli_narrows_from_this_prs_own_range(pr, capsys):
    """The CLI has no merge commit to find a receipt by, so it derives the PR's own tags."""
    tree, rng = pr
    payload = json.dumps({"files": [{"path": RBAC}], "changedFiles": 1})
    argv = ["--json", payload, "--plane", "--range", rng]
    assert land_tags.main(argv, repo=tree.root) == 0
    assert "--tags kubeconfig`" in capsys.readouterr().out


# ── the landing reads the receipt for the tick that crossed its merge commit ───────────

_RBAC_PR = {"files": [{"path": RBAC}], "changedFiles": 1}


def _receipt(manual: dict[str, list[str]]) -> str:
    return json.dumps(
        {"origin": "f" * 40, "base": "e" * 40, "applied": {}, "manual": manual}
    )


def _landing(
    land_run, receipts: str, is_ancestor_rc: int = 0, plane_note=None, ancestry=None
):
    f = Fakes(
        gh_views={"files,changedFiles": _RBAC_PR},
        derived=([], "pr"),
        state={"receipts": receipts},
        is_ancestor_rc=is_ancestor_rc,
        is_ancestor_of=ancestry or {"e" * 40: 1},
    )
    classifier = dataclasses.replace(
        build_classifier(f), plane_note=plane_note or land_tags.plane_note
    )
    return land_run([], f, classifier=classifier)


def test_the_receipts_narrowing_reaches_the_verdict_after_the_tick(land_run):
    _rc, out, _err, _calls, _ = _landing(land_run, _receipt({"k3s": ["kubeconfig"]}))
    assert "ansible/k3s-bringup.yml --tags kubeconfig" in out
    assert "--tags k3s`" not in out


@pytest.mark.parametrize(
    ("receipts", "is_ancestor_rc"),
    [
        ("", 0),
        ("not json", 0),
        (_receipt({"k3s": []}), 0),
        (_receipt({"k3s": ["kubeconfig"]}), 1),
    ],
    ids=["no-receipt", "torn-receipt", "role-not-narrowed", "range-without-this-pr"],
)
def test_without_a_receipt_covering_this_pr_the_verdict_keeps_the_role_tag(
    land_run, receipts, is_ancestor_rc
):
    """The last case is the stale row the sidecar needed a re-derivation to catch: a receipt
    for a range whose origin does not contain this merge commit is somebody else's."""
    _rc, out, _err, _calls, _ = _landing(land_run, receipts, is_ancestor_rc)
    assert "ansible/k3s-bringup.yml --tags k3s`" in out
    assert "--tags kubeconfig" not in out


def test_a_later_ranges_receipt_is_not_quoted_for_this_pr(land_run):
    """The crossing tick wrote no receipt — another session's ff-merge crossed this PR — and
    the next broad tick's receipt has an origin containing it too. Its base contains it as
    well, so that range started past this PR and its tags are somebody else's."""
    _rc, out, _err, _calls, _ = _landing(
        land_run, _receipt({"k3s": ["kubeconfig"]}), ancestry={"e" * 40: 0}
    )
    assert "ansible/k3s-bringup.yml --tags k3s`" in out
    assert "--tags kubeconfig" not in out


def test_a_plane_note_that_raises_after_the_tick_keeps_the_role_tag(land_run):
    """A raise in the re-render ends in the step 1 note, not in a traceback.

    `plane_note` already ran on these inputs in step 1, so this is unlikely. `land.py`
    returning a traceback instead of a VERDICT costs the operator the whole landing; the role
    tag costs them a wider apply.
    """
    calls_made = []

    def plane_note(paths, declared=None, *, quiet=False, narrow_tags=None):
        calls_made.append(narrow_tags)
        if narrow_tags:
            raise RuntimeError("the re-render blew up")
        return land_tags.plane_note(paths, declared, quiet=quiet)

    _rc, out, _err, _calls, _ = _landing(
        land_run, _receipt({"k3s": ["kubeconfig"]}), plane_note=plane_note
    )
    assert calls_made[-1] == {"k3s": frozenset({"kubeconfig"})}, (
        "the re-render was tried"
    )
    assert "VERDICT" in out
    assert "ansible/k3s-bringup.yml --tags k3s`" in out


# ── a landing that deploys its own merge commit narrows from its own range ──────


def test_own_narrowing_is_this_prs_derivation_with_no_row(pr):
    tree, rng = pr
    assert land_tags.own_narrow_tags([RBAC], rng, tree.root) == {
        "k3s": frozenset({"kubeconfig"})
    }


def test_own_narrowing_with_no_range_is_flagged(pr):
    tree, _ = pr
    assert land_tags.own_narrow_tags([RBAC], "", tree.root) == {}


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
    assert "clear-manual-plane k3s --applied kubeconfig" in out
    assert "--tags k3s`" not in out


def test_the_fast_path_keeps_the_role_tag_when_its_derivation_refuses(land_run):
    _rc, out, _err, _calls, _ = _fast_landing(land_run, {})
    assert "ansible/k3s-bringup.yml --tags k3s`" in out
