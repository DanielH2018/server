"""`notice.Tree`: what a revision held, read without the checkout the section walk is bound to.

Every fixture commits through `lib.git_testing`, so a run under prek's hook cannot write the
real repository. A test that opens a revision `Tree` closes it, because the suite turns the
`ResourceWarning` of an unwaited `cat-file` process into an error.
"""

import pytest

from lib.facts.notice import MissingObject, Tree
from lib.git_testing import commit, git_out, init_repo
from lib.repo_paths import REPO

_DOC = "CLAUDE.md"
_V1 = "# Top\n\n## Ports\nSee `a/b.py:PORT`.\n"
_V2 = "# Top\n\n## Ports\nSee `a/b.py:HOST`.\n"


def test_a_revision_reads_its_own_text_and_tracked_set_not_the_checkout(tmp_path):
    repo = init_repo(tmp_path)
    old = commit(repo, "v1", **{_DOC: _V1, "a/b.py": "PORT = 1\n"})
    commit(repo, "v2", **{_DOC: _V2, "a/b.py": None})
    with Tree(repo, old) as tree:
        assert tree.text(_DOC) == _V1
        assert "a/b.py" in tree.tracked
        assert tree.text("a/b.py") == "PORT = 1\n"
    with Tree(repo, "HEAD") as tree:
        assert tree.text(_DOC) == _V2
        assert "a/b.py" not in tree.tracked


def test_the_working_tree_reads_an_uncommitted_edit(tmp_path):
    repo = init_repo(tmp_path)
    commit(repo, "v1", **{_DOC: _V1})
    (repo / _DOC).write_text(_V2)
    assert Tree(repo).text(_DOC) == _V2


def test_the_working_tree_skips_a_listed_file_missing_on_disk(tmp_path):
    repo = init_repo(tmp_path)
    commit(repo, "v1", **{_DOC: _V1, "a/b.py": "PORT = 1\n"})
    (repo / "a/b.py").unlink()
    tree = Tree(repo)
    assert "a/b.py" in tree.tracked
    assert tree.text("a/b.py") is None


@pytest.mark.parametrize("rev", ["HEAD", None], ids=["revision", "working-tree"])
def test_docs_at_every_depth_are_listed(tmp_path, rev):
    """`git ls-tree` does not glob, so a `**/CLAUDE.md` pathspec would list the root doc alone."""
    repo = init_repo(tmp_path)
    commit(
        repo,
        "docs",
        **{_DOC: _V1, "a/b/CLAUDE.md": _V1, "a/NOT_CLAUDE.md": _V1, "a/b.py": ""},
    )
    with Tree(repo, rev) as tree:
        assert tree.docs() == ["CLAUDE.md", "a/b/CLAUDE.md"]


def test_a_path_absent_at_the_revision_reads_as_none_and_sends_no_request(tmp_path):
    """The base side of a file the PR adds: the listing answers, and `cat-file` never starts."""
    repo = init_repo(tmp_path)
    base = commit(repo, "base", **{_DOC: _V1})
    commit(repo, "add", **{"a/b.py": "PORT = 1\n"})
    with Tree(repo, base) as tree:
        assert tree.text("a/b.py") is None
        assert tree.sections("a/b.py") is None
        assert tree._batch is None


def test_a_listed_blob_the_object_store_lacks_raises(tmp_path):
    repo = init_repo(tmp_path)
    commit(repo, "v1", **{_DOC: _V1, "a/b.py": "PORT = 1\n"})
    oid = git_out(repo, "rev-parse", "HEAD:a/b.py")
    (repo / ".git/objects" / oid[:2] / oid[2:]).unlink()
    with Tree(repo, "HEAD") as tree, pytest.raises(MissingObject, match=oid):
        tree.text("a/b.py")


def test_an_inherited_git_dir_does_not_redirect_the_reader(tmp_path, monkeypatch):
    """The hook case: `GIT_DIR` names another repository, and `repo` must still win."""
    other = init_repo(tmp_path / "other")
    commit(other, "other", **{_DOC: "# Other\n"})
    mine = init_repo(tmp_path / "mine")
    rev = commit(mine, "mine", **{_DOC: _V1, "a/b.py": "PORT = 1\n"})
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    with Tree(mine, rev) as tree:
        assert tree.text(_DOC) == _V1
        assert tree.text("a/b.py") == "PORT = 1\n"


def test_an_unchanged_doc_is_split_once_across_revisions(tmp_path):
    repo = init_repo(tmp_path)
    first = commit(repo, "v1", **{_DOC: _V1, "a/b.py": "PORT = 1\n"})
    second = commit(repo, "code only", **{"a/b.py": "PORT = 2\n"})
    third = commit(repo, "v2", **{_DOC: _V2})
    with Tree(repo, first) as a, Tree(repo, second) as b, Tree(repo, third) as c:
        split = a.sections(_DOC)
        changed = c.sections(_DOC)
        assert split is not None and changed is not None
        assert b.sections(_DOC) is split
        assert {s.key for s in split} == {"CLAUDE.md#Top", "CLAUDE.md#Ports"}
        assert [s.body for s in changed] != [s.body for s in split]


def test_the_revision_backend_finds_this_repos_docs():
    """The backend half of the notice's non-vacuity floor, read from this repository's HEAD.

    A reader that listed no docs would make every later notice empty and green. HEAD held 87
    `CLAUDE.md` files on 2026-10-11; a slice that deletes role trees lowers this floor in the
    same PR.
    """
    with Tree(REPO, "HEAD") as tree:
        docs = tree.docs()
        assert len(docs) >= 80
        assert {"CLAUDE.md", "ansible/roles/k8s/home-assistant/CLAUDE.md"} <= set(docs)
        assert tree.sections("CLAUDE.md")
