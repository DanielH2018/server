"""`extract` unpacks the named top-level trees a commit holds and skips the ones it lacks.

The cross-plane gate renders a commit from `k8s`, `inventory`, `secrets`, `scripts` and `hosts`
(H7.2e: the k8s templates and `longhorn_lib` read `hosts/lib/`), or from `ansible` for a base
before ADR 0030's rename. A base commit older than `hosts/` must not make
`git archive` fail on a path it does not hold, and a commit that holds none of the named trees
must unpack nothing rather than the whole commit.

Run: uv run pytest scripts/validate/tests/test_cross_plane_extract.py
"""

from pathlib import Path

import pytest
from lib.git import extract
from lib.git_testing import commit, init_repo
from validate import render_diff_gates as gate

TREES = gate.RENDER_TREES


@pytest.fixture
def repo(tmp_path) -> Path:
    return init_repo(tmp_path / "repo")


def test_a_commit_without_hosts_unpacks_the_trees_it_holds(repo, tmp_path):
    sha = commit(repo, "c", **{"k8s/a.yml": "a", "scripts/s.py": "s", "docs/d.md": "d"})
    extract(sha, tmp_path / "out", TREES, repo=repo)
    assert (tmp_path / "out/k8s/a.yml").is_file()
    assert (tmp_path / "out/scripts/s.py").is_file()
    assert not (tmp_path / "out/docs").exists()


def test_a_commit_with_hosts_unpacks_it_too(repo, tmp_path):
    sha = commit(repo, "c", **{"k8s/a.yml": "a", "hosts/lib/host_lib.py": "h"})
    extract(sha, tmp_path / "out", TREES, repo=repo)
    assert (tmp_path / "out/hosts/lib/host_lib.py").is_file()


def test_a_commit_holding_none_of_the_named_trees_unpacks_nothing(repo, tmp_path):
    sha = commit(repo, "c", **{"docs/d.md": "d"})
    extract(sha, tmp_path / "out", ("k8s", "hosts"), repo=repo)
    assert list((tmp_path / "out").iterdir()) == []


PRE_RENAME = {
    "ansible/roles/k8s/web/defaults/main.yml": "a: 1",
    "ansible/inventory/group_vars/all.yml": "b: 2",
}
POST_RENAME = {
    "k8s/roles/web/defaults/main.yml": "a: 1",
    "inventory/group_vars/all.yml": "b: 2",
}


@pytest.mark.parametrize("files", [PRE_RENAME, POST_RENAME], ids=["pre", "post"])
def test_a_commit_on_either_side_of_the_rename_yields_its_roles_and_group_vars(
    repo, tmp_path, files
):
    sha = commit(repo, "c", **files)
    extract(sha, tmp_path / "out", TREES, repo=repo)
    roles, group_vars = gate.layout(tmp_path / "out")
    assert [p.name for p in roles.iterdir()] == ["web"]
    assert group_vars.read_text() == "b: 2"
