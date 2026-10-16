"""The replay's tree preparation: the ownership rewrite, the transplant and the reversal.

Run: uv run pytest scripts/dev/tests/test_cross_plane_replay.py
"""

import pytest

from cross_plane_replay import RenderFailed, prepare_tree, set_flux_owned
from lib.git_testing import commit, init_repo

TEMPLATE = "ansible/templates/shell.j2"
INVENTORY = "ansible/inventory/hosts.yml"


def test_set_flux_owned_leaves_exactly_the_named_entries_on_flux(tmp_path):
    host_vars = tmp_path / "daniel-box.yml"
    host_vars.write_text(
        "containers_list:\n"
        "  - name: bento-pdf\n    platform: k8s\n"
        "  - name: texbrain\n    platform: k8s\n    owner: flux\n"
    )
    set_flux_owned(host_vars, ["bento-pdf"])
    assert host_vars.read_text() == (
        "containers_list:\n"
        "  - name: bento-pdf\n    owner: flux\n    platform: k8s\n"
        "  - name: texbrain\n    platform: k8s\n"
    )


@pytest.fixture
def repo(tmp_path):
    """A history whose middle commit edits the template; HEAD then edits the inventory."""
    repo = init_repo(tmp_path / "repo")
    commit(
        repo, "base", **{TEMPLATE: "limit: 3\n", INVENTORY: "a\n", "scripts/r.py": ""}
    )
    edit = commit(repo, "edit", **{TEMPLATE: "limit: 5\n"})
    commit(repo, "later", **{INVENTORY: "b\n"})
    return repo, edit


def test_a_transplant_takes_the_commit_s_paths_and_keeps_head_elsewhere(tmp_path, repo):
    repo, edit = repo
    tree = tmp_path / "tree"
    prepare_tree(f"{edit}^", [TEMPLATE], tree, repo=repo)
    assert (tree / TEMPLATE).read_text() == "limit: 3\n"
    assert (tree / INVENTORY).read_text() == "b\n"


def test_an_undo_reverses_only_the_commit_s_own_change(tmp_path, repo):
    repo, edit = repo
    tree = tmp_path / "tree"
    prepare_tree(edit, [TEMPLATE], tree, undo=True, repo=repo)
    assert (tree / TEMPLATE).read_text() == "limit: 3\n"
    assert (tree / INVENTORY).read_text() == "b\n"


def test_an_undo_that_no_longer_applies_is_rejected(tmp_path, repo):
    repo, edit = repo
    commit(repo, "rewrite", **{TEMPLATE: "limit: 9\n"})
    with pytest.raises(RenderFailed, match="no longer reverses"):
        prepare_tree(edit, [TEMPLATE], tmp_path / "tree", undo=True, repo=repo)
