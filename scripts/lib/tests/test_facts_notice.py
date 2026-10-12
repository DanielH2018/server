"""`notice.Tree`: what a revision held, read without the checkout the section walk is bound to.

Every fixture commits through `lib.git_testing`, so a run under prek's hook cannot write the
real repository. A test that opens a revision `Tree` closes it, because the suite turns the
`ResourceWarning` of an unwaited `cat-file` process into an error.
"""

import pytest

from lib.facts.notice import MissingObject, Notice, Tree, moved_sections
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


def _doc(*cites: str) -> str:
    return "# Top\n\n## Ports\n" + "".join(f"See `{c}`.\n" for c in cites)


def _notice(tmp_path, base: dict, head: dict) -> list[Notice]:
    """Commit ``base``, then ``head`` on top of it, and list the notice between the two."""
    repo = init_repo(tmp_path)
    old = commit(repo, "base", **base)
    new = commit(repo, "head", **head)
    return moved_sections(repo, old, new)


def _row(atom: str, form: str, kind: str, edited: bool = False) -> Notice:
    return Notice("CLAUDE.md#Ports", atom, form, kind, edited)


def test_a_content_atom_whose_value_changed_is_moved(tmp_path):
    rows = _notice(
        tmp_path,
        {_DOC: _doc("a/b.py:PORT"), "a/b.py": "PORT = 1\n"},
        {"a/b.py": "PORT = 2\n"},
    )
    assert rows == [_row("a/b.py:PORT", "symbol", "moved")]


@pytest.mark.parametrize(
    ("cite", "before", "after"),
    [
        ("a/b.py:PORT", "PORT = 1\n", "PORT = 1\nHOST = 'h'\n"),
        (
            "a/b.py:port",
            'def port():\n    """One."""\n    return 1\n',
            'def port():\n    """The listen port."""\n    return 1\n',
        ),
    ],
    ids=["another-symbol", "docstring-only"],
)
def test_a_touched_file_whose_atom_did_not_change_gives_no_row(
    tmp_path, cite, before, after
):
    """What the notice exists for: the lock would flag a rehash, the notice compares ends."""
    rows = _notice(tmp_path, {_DOC: _doc(cite), "a/b.py": before}, {"a/b.py": after})
    assert rows == []


def test_a_citation_the_diff_did_not_touch_is_never_read(tmp_path):
    """The marker is ambiguous at both ends, so reading it at all would list a row."""
    twice = "# DECIDED: keep one\n# DECIDED: keep two\n"
    rows = _notice(
        tmp_path,
        {_DOC: _doc("a/b.py:DECIDED: keep"), "a/b.py": twice, "c.txt": "1\n"},
        {"c.txt": "2\n"},
    )
    assert rows == []


@pytest.mark.parametrize(
    "after", ["HOST = 1\n", None], ids=["symbol-removed", "file-deleted"]
)
def test_a_content_atom_that_resolves_at_the_base_only_is_gone(tmp_path, after):
    """A deleted file is tracked at the base, so its citation survives to give this row."""
    rows = _notice(
        tmp_path,
        {_DOC: _doc("a/b.py:PORT"), "a/b.py": "PORT = 1\n", "c.txt": ""},
        {"a/b.py": after},
    )
    assert rows == [_row("a/b.py:PORT", "symbol", "gone")]


@pytest.mark.parametrize(
    "before", ["HOST = 1\n", None], ids=["symbol-added", "file-added"]
)
def test_a_content_atom_that_resolves_at_the_head_only_gives_no_row(tmp_path, before):
    base = {_DOC: _doc("a/b.py:PORT")}
    if before is not None:
        base["a/b.py"] = before
    rows = _notice(tmp_path, base, {"a/b.py": "HOST = 1\nPORT = 1\n"})
    assert rows == []


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        ({"a/b.py": "PORT = 2\n"}, []),
        ({"a/b.py": None}, [_row("a/b.py", "path", "gone")]),
        ({"a/b.py": None, "a/c.py": "PORT = 1\n"}, [_row("a/b.py", "path", "gone")]),
    ],
    ids=["edited", "deleted", "renamed"],
)
def test_a_path_atom_lists_only_when_it_goes(tmp_path, head, expected):
    """A path atom hashes existence in the lock, so an edit never moves it."""
    base = {_DOC: _doc("a/b.py"), "a/b.py": "PORT = 1\n", "c.txt": ""}
    assert _notice(tmp_path, base, head) == expected


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        ({"d/e/f.py": None}, []),
        ({"d/e/f.py": None, "d/e/g.py": None}, [_row("d/e/", "path", "gone")]),
    ],
    ids=["one-file-left", "emptied"],
)
def test_a_directory_citation_is_gone_once_no_file_under_it_remains(
    tmp_path, head, expected
):
    base = {_DOC: _doc("d/e/"), "d/e/f.py": "", "d/e/g.py": "", "c.txt": ""}
    assert _notice(tmp_path, base, head) == expected


def test_a_marker_prefix_that_matches_twice_is_ambiguous(tmp_path):
    rows = _notice(
        tmp_path,
        {_DOC: _doc("a/b.py:DECIDED: keep"), "a/b.py": "# DECIDED: keep one\n"},
        {"a/b.py": "# DECIDED: keep one\n# DECIDED: keep two\n"},
    )
    assert rows == [_row("a/b.py:DECIDED: keep", "marker", "ambiguous")]


@pytest.mark.parametrize(
    ("cite", "path", "before", "after", "form"),
    [
        ("a/b.py:PORT", "a/b.py", "PORT = 1\n", "PORT = (\n", "symbol"),
        ("a/c.yml:port", "a/c.yml", "port: 1\n", "port: [\n", "yaml"),
    ],
    ids=["python", "yaml"],
)
def test_a_cited_file_that_does_not_parse_is_unparsable(
    tmp_path, cite, path, before, after, form
):
    rows = _notice(tmp_path, {_DOC: _doc(cite), path: before}, {path: after})
    assert rows == [_row(cite, form, "unparsable")]


_TWO_SECTIONS = _doc("a/b.py:PORT") + "\n## Other\nUnrelated.\n"


@pytest.mark.parametrize(
    ("doc_head", "edited"),
    [
        (_TWO_SECTIONS.replace("PORT`.", "PORT`, the listen port."), True),
        (_TWO_SECTIONS.replace("Unrelated.", "Still unrelated."), False),
    ],
    ids=["citing-section-edited", "other-section-edited"],
)
def test_prose_edited_says_whether_the_diff_changed_the_citing_section(
    tmp_path, doc_head, edited
):
    rows = _notice(
        tmp_path,
        {_DOC: _TWO_SECTIONS, "a/b.py": "PORT = 1\n"},
        {_DOC: doc_head, "a/b.py": "PORT = 2\n"},
    )
    assert rows == [_row("a/b.py:PORT", "symbol", "moved", edited)]


def test_each_citing_section_lists_the_atom_once(tmp_path):
    doc = _doc("a/b.py:PORT", "a/b.py:PORT") + "\n## Hosts\nSee `a/b.py:PORT`.\n"
    rows = _notice(
        tmp_path,
        {_DOC: doc, "a/b.py": "PORT = 1\n"},
        {"a/b.py": "PORT = 2\n"},
    )
    assert [r.unit for r in rows] == ["CLAUDE.md#Ports", "CLAUDE.md#Hosts"]


def test_with_no_head_the_working_tree_is_the_head(tmp_path):
    repo = init_repo(tmp_path)
    base = commit(repo, "base", **{_DOC: _doc("a/b.py:PORT"), "a/b.py": "PORT = 1\n"})
    (repo / "a/b.py").write_text("PORT = 2\n")
    assert moved_sections(repo, base) == [_row("a/b.py:PORT", "symbol", "moved")]
