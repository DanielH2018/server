"""The `moved` finding names the identifiers its change removed that the section's prose names.

Each fixture reproduces the shape of a move from the lock's history (#4255). The verdict never
changes: every finding here stays `moved`, and only the detail grows.
"""

from lib.facts import removed
from lib.facts.lock import LOCK_REL, check_lock, verify_units
from lib.git_testing import commit, git, init_repo

_UNIT = "CLAUDE.md#Window"


def _moved(repo, files, doc):
    """Verify ``doc`` at a first commit of ``files``, then return the finding after ``commit``."""
    sha = commit(repo, "base", **{**files, "CLAUDE.md": doc})
    verify_units(repo, repo / LOCK_REL, [_UNIT], sha[:9])
    return sha[:9]


def _detail(repo):
    findings = check_lock(repo, repo / LOCK_REL)
    assert [f.kind for f in findings] == ["moved"]
    return findings[0].detail


# a91ed2c6d: the cited test only renamed a local; the same commit renamed the defaults key.
_A91_DOC = (
    "## Window\nThe window runs in `kuma_maintenance_window_timezone`, "
    "ENFORCED by `k/tests/test_w.py::test_window`.\n"
)
_A91_TEST = "def test_window():\n    {name} = '25 7 * * 0'\n    assert {name}\n"
_A91_BEFORE = {
    "k/defaults/main.yml": "kuma_maintenance_window_timezone: Atlantic/Reykjavik\n",
    "k/tests/test_w.py": _A91_TEST.format(name="kuma_maintenance_sync_schedule"),
}
_A91_AFTER = {
    "k/defaults/main.yml": "kuma_maint_tz: Atlantic/Reykjavik\n",
    "k/tests/test_w.py": _A91_TEST.format(name="kuma_sync_sched"),
}
_A91_NAMED = "; the section names `kuma_maintenance_window_timezone` (removed in {}..HEAD, and gone from the tree)"


def test_a_renamed_key_the_section_names_is_named_from_the_range(tmp_path):
    repo = init_repo(tmp_path)
    sha = _moved(repo, _A91_BEFORE, _A91_DOC)
    commit(repo, "rename", **_A91_AFTER)
    assert _detail(repo).endswith(_A91_NAMED.format(sha))


def test_a_verified_sha_squashed_off_head_falls_back_to_the_lock_commit(tmp_path):
    """The squash shape: the row names a branch commit HEAD never held, and the squash commit
    that carries the code and the lock is the base the evidence diffs from."""
    repo = init_repo(tmp_path)
    for rel, text in {**_A91_BEFORE, "CLAUDE.md": _A91_DOC}.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    git(repo, "add", "-A")
    verify_units(repo, repo / LOCK_REL, [_UNIT], "feedface0")
    squash = commit(repo, "squash of the branch that verified")[:9]
    commit(repo, "rename", **_A91_AFTER)
    assert _detail(repo).endswith(_A91_NAMED.format(squash))


def test_a_name_the_atom_stopped_using_is_named_from_the_atom(tmp_path):
    """e66aca65e: the function stopped pointing at a page the section also names."""
    repo = init_repo(tmp_path)
    doc = "## Window\n`t/m.py:target` writes `docs/reference/scripts.md`.\n"
    _moved(
        repo,
        {
            "t/m.py": "def target():\n    return 'docs/reference/scripts.md'\n",
            "docs/reference/scripts.md": "# scripts\n",
        },
        doc,
    )
    commit(repo, "repoint", **{"t/m.py": "def target():\n    return 'docs/site.md'\n"})
    assert _detail(repo).endswith(
        "; the section names `docs/reference/scripts.md` (the atom's change removed it)"
    )


def test_a_refactor_the_prose_does_not_name_says_none(tmp_path):
    repo = init_repo(tmp_path)
    doc = "## Window\n`t/m.py:limit` caps the window.\n"
    sha = _moved(
        repo, {"t/m.py": "def limit():\n    old_value = 3\n    return old_value\n"}, doc
    )
    commit(
        repo,
        "rename a local",
        **{"t/m.py": "def limit():\n    cap_value = 3\n    return cap_value\n"},
    )
    detail = _detail(repo)
    assert detail.startswith("recorded ")
    assert detail.endswith(
        "; none of the 1 identifiers the atom's change removed appears in the section, "
        f"and none it names left the tree in {sha}..HEAD"
    )


def test_no_commit_on_head_holding_the_recorded_atom_keeps_the_plain_message(tmp_path):
    """The row's sha does not resolve, and the one commit that wrote its hash into the lock
    already holds the moved value, so no commit on HEAD hashes the atom as recorded."""
    repo = init_repo(tmp_path)
    doc = "## Window\n`t/m.py:LIMIT` caps the window.\n"
    commit(repo, "base", **{"t/m.py": "LIMIT = 3\n", "CLAUDE.md": doc})
    verify_units(repo, repo / LOCK_REL, [_UNIT], "0000000ff")
    commit(repo, "bump", **{"t/m.py": "LIMIT = 4\n"})
    detail = _detail(repo)
    assert detail.startswith("recorded ") and ";" not in detail


def test_the_token_collector_excludes_the_lock():
    """`removed.LOCK_PATH` restates `LOCK_REL` because importing it would be a cycle."""
    assert removed.LOCK_PATH == LOCK_REL
    assert f":!{LOCK_REL}" in removed._CODE
