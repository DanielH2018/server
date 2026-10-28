"""`extract` unpacks the named top-level trees a commit holds and skips the ones it lacks.

The cross-plane gate renders a commit from `ansible`, `scripts` and `hosts` (H7.2e: the k8s
templates and `longhorn_lib` read `hosts/lib/`). A base commit older than `hosts/` must not make
`git archive` fail on a path it does not hold, and a commit that holds none of the named trees
must unpack nothing rather than the whole commit.

Run: uv run pytest scripts/validate/tests/test_cross_plane_extract.py
"""

from pathlib import Path

import pytest
from lib.git_testing import commit, init_repo
from validate import render_diff_gates as gate

TREES = ("ansible", "scripts", "hosts")


@pytest.fixture
def repo(tmp_path) -> Path:
    return init_repo(tmp_path / "repo")


def test_a_commit_without_hosts_unpacks_the_trees_it_holds(repo, tmp_path):
    sha = commit(
        repo, "c", **{"ansible/a.yml": "a", "scripts/s.py": "s", "docs/d.md": "d"}
    )
    gate.extract(sha, tmp_path / "out", TREES, repo=repo)
    assert (tmp_path / "out/ansible/a.yml").is_file()
    assert (tmp_path / "out/scripts/s.py").is_file()
    assert not (tmp_path / "out/docs").exists()


def test_a_commit_with_hosts_unpacks_it_too(repo, tmp_path):
    sha = commit(repo, "c", **{"ansible/a.yml": "a", "hosts/lib/host_lib.py": "h"})
    gate.extract(sha, tmp_path / "out", TREES, repo=repo)
    assert (tmp_path / "out/hosts/lib/host_lib.py").is_file()


def test_a_commit_holding_none_of_the_named_trees_unpacks_nothing(repo, tmp_path):
    sha = commit(repo, "c", **{"docs/d.md": "d"})
    gate.extract(sha, tmp_path / "out", ("ansible", "hosts"), repo=repo)
    assert list((tmp_path / "out").iterdir()) == []
