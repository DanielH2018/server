"""facts.lock: written by the tool, checksummed against a hand edit, and checked against the tree."""

import json

import pytest

from lib.git_testing import commit, git, init_repo
from lib.facts.lock import (
    FINDING_KINDS,
    LOCK_REL,
    _PYTHON,
    Finding,
    build_repo_edb,
    check_lock,
    forget_units,
    lock_at,
    lock_tampered,
    read_lock,
    repo_citations,
    reverify_benign,
    row_file_name,
    verify_units,
    write_lock,
)
from lib.facts.relations import derive, status_of

DOC = """# Root

## Gate
`{repo}m.py:LIMIT` bounds it, ENFORCED by `t/test_m.py::test_limit`.

## Style
Write commit messages that explain why.
"""


def _repo(tmp_path):
    init_repo(tmp_path)
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "m.py").write_text("LIMIT = 85\n")
    (tmp_path / "t" / "test_m.py").write_text(
        "def test_limit():\n    # fact: CLAUDE.md#Gate\n    assert True\n"
    )
    (tmp_path / "CLAUDE.md").write_text(DOC.format(repo="t/"))
    (tmp_path / "docs").mkdir()
    git(tmp_path, "add", "-A")
    return tmp_path


def _git_add(repo, *paths):
    """Track ``paths`` in ``repo`` — a fixture step for a file added after ``_repo``'s commit."""
    git(repo, "add", *paths)


def test_lock_round_trips_and_is_absent_as_empty(tmp_path):
    p = tmp_path / "facts.lock"
    assert read_lock(p) == {}
    units = {"CLAUDE.md#Gate": {"verified_sha": "abc", "atoms": {"m.py:LIMIT": "h"}}}
    write_lock(p, units)
    assert read_lock(p) == units
    assert not lock_tampered(p)


def test_hand_edited_lock_is_flagged(tmp_path):
    p = tmp_path / "facts.lock"
    write_lock(
        p, {"CLAUDE.md#Gate": {"verified_sha": "abc", "atoms": {"m.py:LIMIT": "h"}}}
    )
    row = p / row_file_name("CLAUDE.md#Gate")
    row.write_text(row.read_text().replace('"h"', '"hh"'))
    assert lock_tampered(p)


def test_verify_then_check_is_clean(tmp_path):
    repo = _repo(tmp_path)
    verify_units(repo, repo / LOCK_REL, ["CLAUDE.md#Gate"], "abc1234")
    lock = read_lock(repo / LOCK_REL)
    assert set(lock["CLAUDE.md#Gate"]["atoms"]) == {
        "t/m.py:LIMIT",
        "t/test_m.py::test_limit",
    }
    assert lock["CLAUDE.md#Gate"]["verified_sha"] == "abc1234"
    assert check_lock(repo, repo / LOCK_REL) == []


def test_out_of_tree_citation_is_not_in_the_edb(tmp_path):
    repo = _repo(tmp_path)
    (repo / "CLAUDE.md").write_text(
        DOC.format(repo="t/").replace(
            "bounds it", "bounds it on `origin/master`, see `defaults/main.yml`"
        )
    )
    verify_units(repo, repo / LOCK_REL, ["CLAUDE.md#Gate"], "abc1234")
    assert set(read_lock(repo / LOCK_REL)["CLAUDE.md#Gate"]["atoms"]) == {
        "t/m.py:LIMIT",
        "t/test_m.py::test_limit",
    }
    edb = build_repo_edb(repo, read_lock(repo / LOCK_REL))
    assert not {a for _u, a in edb.cites if "origin" in a or "defaults" in a}
    assert check_lock(repo, repo / LOCK_REL) == []


def test_moved_atom_is_flagged(tmp_path):
    repo = _repo(tmp_path)
    verify_units(repo, repo / LOCK_REL, ["CLAUDE.md#Gate"], "abc1234")
    (repo / "t" / "m.py").write_text("LIMIT = 90\n")
    kinds = {(f.unit, f.atom, f.kind) for f in check_lock(repo, repo / LOCK_REL)}
    assert kinds == {("CLAUDE.md#Gate", "t/m.py:LIMIT", "moved")}


def test_missing_atom_is_flagged(tmp_path):
    repo = _repo(tmp_path)
    verify_units(repo, repo / LOCK_REL, ["CLAUDE.md#Gate"], "abc1234")
    (repo / "t" / "m.py").write_text("OTHER = 1\n")
    assert {f.kind for f in check_lock(repo, repo / LOCK_REL)} == {"missing"}


def test_renamed_section_is_flagged_as_gone(tmp_path):
    repo = _repo(tmp_path)
    verify_units(repo, repo / LOCK_REL, ["CLAUDE.md#Gate"], "abc1234")
    (repo / "CLAUDE.md").write_text(
        DOC.format(repo="t/").replace("## Gate", "## The gate")
    )
    assert {f.kind for f in check_lock(repo, repo / LOCK_REL)} == {"section-gone"}


def test_atom_dropped_from_prose_is_flagged(tmp_path):
    repo = _repo(tmp_path)
    verify_units(repo, repo / LOCK_REL, ["CLAUDE.md#Gate"], "abc1234")
    (repo / "CLAUDE.md").write_text(
        DOC.format(repo="t/").replace(", ENFORCED by `t/test_m.py::test_limit`", "")
    )
    assert {f.kind for f in check_lock(repo, repo / LOCK_REL)} == {
        "atom-no-longer-cited"
    }


def test_tampered_lock_is_the_only_finding(tmp_path):
    repo = _repo(tmp_path)
    verify_units(repo, repo / LOCK_REL, ["CLAUDE.md#Gate"], "abc1234")
    p = repo / LOCK_REL
    row = p / row_file_name("CLAUDE.md#Gate")
    row.write_text(row.read_text().replace("abc1234", "zzz9999"))
    assert [f.kind for f in check_lock(repo, p)] == ["lock-tampered"]


def test_repo_edb_gives_statuses(tmp_path):
    repo = _repo(tmp_path)
    verify_units(repo, repo / LOCK_REL, ["CLAUDE.md#Gate"], "abc1234")
    edb = build_repo_edb(repo, read_lock(repo / LOCK_REL))
    idb = derive(edb)
    assert status_of(idb, "CLAUDE.md#Gate") == "IN"
    assert status_of(idb, "CLAUDE.md#Style") == "CONVENTION"
    assert ("t/test_m.py::test_limit", "CLAUDE.md#Gate") in edb.backref
    assert not idb.one_way


def test_unverified_section_with_citations_is_unverified(tmp_path):
    repo = _repo(tmp_path)
    edb = build_repo_edb(repo, {})
    assert status_of(derive(edb), "CLAUDE.md#Gate") == "UNVERIFIED"


def test_new_citation_on_a_locked_unit_is_flagged(tmp_path):
    """The clean twin is ``test_verify_then_check_is_clean``."""
    repo = _repo(tmp_path)
    verify_units(repo, repo / LOCK_REL, ["CLAUDE.md#Gate"], "abc1234")
    (repo / "t" / "other.py").write_text("CAP = 3\n")
    _git_add(repo, "t/other.py")
    (repo / "CLAUDE.md").write_text(
        DOC.format(repo="t/").replace(
            "bounds it", "bounds it, `t/other.py:CAP` caps it"
        )
    )
    assert {(f.atom, f.kind) for f in check_lock(repo, repo / LOCK_REL)} == {
        ("t/other.py:CAP", "unrecorded-atom")
    }


def test_ambiguous_marker_is_flagged(tmp_path):
    repo = _repo(tmp_path)
    (repo / "t" / "m.py").write_text("LIMIT = 85\n# DECIDED: keep the cap\n")
    (repo / "CLAUDE.md").write_text(
        DOC.format(repo="t/").replace("bounds it", "bounds it, `t/m.py:DECIDED: keep`")
    )
    verify_units(repo, repo / LOCK_REL, ["CLAUDE.md#Gate"], "abc1234")
    (repo / "t" / "m.py").write_text(
        "LIMIT = 85\n# DECIDED: keep the cap\n# DECIDED: keep the floor\n"
    )
    assert [f.kind for f in check_lock(repo, repo / LOCK_REL)] == ["ambiguous"]


def test_forget_removes_the_row_and_rechecksums(tmp_path):
    repo = _repo(tmp_path)
    verify_units(repo, repo / LOCK_REL, ["CLAUDE.md#Gate"], "abc1234")
    assert forget_units(repo / LOCK_REL, ["CLAUDE.md#Gate"]) == {}
    assert read_lock(repo / LOCK_REL) == {}
    assert not lock_tampered(repo / LOCK_REL)
    with pytest.raises(KeyError):
        forget_units(repo / LOCK_REL, ["CLAUDE.md#Gate"])


def test_verify_reports_the_atoms_it_skipped(tmp_path):
    repo = _repo(tmp_path)
    (repo / "CLAUDE.md").write_text(
        DOC.format(repo="t/").replace("bounds it", "bounds it, `t/m.py:GONE` too")
    )
    lock, skipped = verify_units(repo, repo / LOCK_REL, ["CLAUDE.md#Gate"], "abc1234")
    assert skipped == ["t/m.py:GONE"]
    assert "t/m.py:GONE" not in lock["CLAUDE.md#Gate"]["atoms"]


def test_a_row_recorded_under_this_interpreter_is_compared(tmp_path):
    repo = _repo(tmp_path)
    verify_units(repo, repo / LOCK_REL, ["CLAUDE.md#Gate"], "abc1234")
    assert read_lock(repo / LOCK_REL)["CLAUDE.md#Gate"]["python"] == _PYTHON
    (repo / "t" / "m.py").write_text("LIMIT = 90\n")
    assert [f.kind for f in check_lock(repo, repo / LOCK_REL)] == ["moved"]


def test_a_row_recorded_under_another_interpreter_is_flagged_and_not_compared(tmp_path):
    repo = _repo(tmp_path)
    verify_units(repo, repo / LOCK_REL, ["CLAUDE.md#Gate"], "abc1234")
    lock = read_lock(repo / LOCK_REL)
    lock["CLAUDE.md#Gate"]["python"] = "3.9"
    write_lock(repo / LOCK_REL, lock)
    (repo / "t" / "m.py").write_text("LIMIT = 90\n")
    findings = check_lock(repo, repo / LOCK_REL)
    assert [f.kind for f in findings] == ["interpreter-moved"]
    assert "3.9" in findings[0].detail and _PYTHON in findings[0].detail


def test_a_finding_kind_outside_the_census_is_flagged():
    """Every Finding above is the clean side: the census must bind its producers too."""
    with pytest.raises(ValueError, match="FINDING_KINDS"):
        Finding("u", "a", "nope", "d")


def test_finding_kinds_census():
    assert FINDING_KINDS == frozenset(
        {
            "moved",
            "missing",
            "section-gone",
            "atom-no-longer-cited",
            "unrecorded-atom",
            "interpreter-moved",
            "lock-tampered",
            "ambiguous",
            "empty-row",
        }
    )


def test_a_reader_given_the_parsed_citations_reads_them_not_the_tree(tmp_path):
    """The seam `fact_status.py status` uses to parse the docs once for all three readers.

    A `by_unit` naming no section is the red-proof: every reader that still parsed for
    itself would find the tree's sections and report nothing wrong.
    """
    repo = _repo(tmp_path)
    verify_units(repo, repo / LOCK_REL, ["CLAUDE.md#Gate"], "abc1234")
    assert check_lock(repo, repo / LOCK_REL) == []
    assert [f.kind for f in check_lock(repo, repo / LOCK_REL, {})] == ["section-gone"]
    assert build_repo_edb(repo, read_lock(repo / LOCK_REL), {}).cites == frozenset()
    with pytest.raises(KeyError):
        verify_units(repo, repo / LOCK_REL, ["CLAUDE.md#Gate"], "abc1234", {})
    parsed = repo_citations(repo)
    assert check_lock(repo, repo / LOCK_REL, parsed) == []
    assert {u for u, _ in build_repo_edb(repo, {}, parsed).cites} == {"CLAUDE.md#Gate"}


def _add_a_citation(repo):
    """Edit the Gate section's prose to cite one more atom — the benign edit."""
    (repo / "t" / "other.py").write_text("CAP = 3\n")
    _git_add(repo, "t/other.py")
    (repo / "CLAUDE.md").write_text(
        DOC.format(repo="t/").replace(
            "bounds it", "bounds it, `t/other.py:CAP` caps it"
        )
    )


def test_a_prose_edit_that_adds_a_citation_is_reverified(tmp_path):
    repo = _repo(tmp_path)
    verify_units(repo, repo / LOCK_REL, ["CLAUDE.md#Gate"], "abc1234")
    _add_a_citation(repo)

    done, blocking = reverify_benign(
        repo, repo / LOCK_REL, {"CLAUDE.md#Gate"}, "def5678"
    )

    assert (done, blocking) == (["CLAUDE.md#Gate"], [])
    assert not check_lock(repo, repo / LOCK_REL)
    assert read_lock(repo / LOCK_REL)["CLAUDE.md#Gate"]["verified_sha"] == "def5678"


def test_a_moved_atom_is_refused_even_when_the_prose_changed(tmp_path):
    """The contract: a re-hash of content drift stays a human's call."""
    repo = _repo(tmp_path)
    verify_units(repo, repo / LOCK_REL, ["CLAUDE.md#Gate"], "abc1234")
    _add_a_citation(repo)
    (repo / "t" / "m.py").write_text("LIMIT = 86\n")

    done, blocking = reverify_benign(
        repo, repo / LOCK_REL, {"CLAUDE.md#Gate"}, "def5678"
    )

    assert done == []
    assert [f.kind for f in blocking] == ["moved"]
    assert read_lock(repo / LOCK_REL)["CLAUDE.md#Gate"]["verified_sha"] == "abc1234"


def test_a_section_the_commit_did_not_edit_is_left_alone(tmp_path):
    repo = _repo(tmp_path)
    verify_units(repo, repo / LOCK_REL, ["CLAUDE.md#Gate"], "abc1234")
    _add_a_citation(repo)

    done, blocking = reverify_benign(repo, repo / LOCK_REL, set(), "def5678")

    assert done == []
    assert blocking == []  # the finding is benign; it is just not this commit's to fold
    assert read_lock(repo / LOCK_REL)["CLAUDE.md#Gate"]["verified_sha"] == "abc1234"


def test_a_tampered_lock_is_never_reverified_over(tmp_path):
    repo = _repo(tmp_path)
    verify_units(repo, repo / LOCK_REL, ["CLAUDE.md#Gate"], "abc1234")
    lock_path = repo / LOCK_REL
    row = lock_path / row_file_name("CLAUDE.md#Gate")
    row.write_text(
        row.read_text().replace('"verified_sha": "abc1234"', '"verified_sha": "zzz"')
    )

    done, blocking = reverify_benign(repo, lock_path, {"CLAUDE.md#Gate"}, "def5678")

    assert done == []
    assert [f.kind for f in blocking] == ["lock-tampered"]
    assert lock_tampered(lock_path)


def test_a_citation_the_author_deleted_is_reverified(tmp_path):
    repo = _repo(tmp_path)
    verify_units(repo, repo / LOCK_REL, ["CLAUDE.md#Gate"], "abc1234")
    (repo / "CLAUDE.md").write_text(
        DOC.format(repo="t/").replace(", ENFORCED by `t/test_m.py::test_limit`", "")
    )

    done, blocking = reverify_benign(
        repo, repo / LOCK_REL, {"CLAUDE.md#Gate"}, "def5678"
    )

    assert (done, blocking) == (["CLAUDE.md#Gate"], [])
    assert (
        "t/test_m.py::test_limit"
        not in read_lock(repo / LOCK_REL)["CLAUDE.md#Gate"]["atoms"]
    )


def test_a_cited_file_leaving_the_index_is_refused(tmp_path):
    """Untracking a cited file drops the same atom the author would have dropped.

    The sentence still names the file, so the claim is now stale and a person reads it.
    """
    repo = _repo(tmp_path)
    verify_units(repo, repo / LOCK_REL, ["CLAUDE.md#Gate"], "abc1234")
    git(repo, "rm", "-q", "-f", "--cached", "t/test_m.py")
    (repo / "CLAUDE.md").write_text(
        DOC.format(repo="t/").replace("bounds it", "still bounds it")
    )

    done, blocking = reverify_benign(
        repo, repo / LOCK_REL, {"CLAUDE.md#Gate"}, "def5678"
    )

    assert done == []
    assert [f.kind for f in blocking] == ["atom-no-longer-cited"]
    assert read_lock(repo / LOCK_REL)["CLAUDE.md#Gate"]["verified_sha"] == "abc1234"


def _row(sha, h):
    return {"atoms": {"t/m.py:LIMIT": h}, "python": _PYTHON, "verified_sha": sha}


_A, _B, _B2, _C, _D = (f"CLAUDE.md#{h}" for h in ("A", "B", "B2", "C", "D"))
_BASE = {_A: _row("b0", "a0"), _C: _row("b0", "c0")}
# Each shape is two branches' locks from `_BASE`. All three conflicted in one file: the first
# on a top-level checksum, the other two on rows a single sorted file put next to each other.
_SHAPES = {
    "reverify-and-insert-between": (
        {**_BASE, _A: _row("r1", "a1")},
        {**_BASE, _B: _row("d1", "b1"), _C: _row("d1", "c1")},
    ),
    "two-inserts-in-one-gap": (
        {**_BASE, _B: _row("r1", "b1")},
        {**_BASE, _B2: _row("d1", "b2")},
    ),
    "append-after-a-reverified-last-row": (
        {**_BASE, _C: _row("r1", "c1")},
        {**_BASE, _D: _row("d1", "d1")},
    ),
}


@pytest.mark.parametrize("swap", [False, True], ids=["one-first", "two-first"])
@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_two_branches_verifying_different_sections_merge_cleanly(tmp_path, shape, swap):
    # Issue #5341: two PRs that verified different sections conflicted on the lock.
    repo = init_repo(tmp_path)
    lock_path = repo / LOCK_REL
    write_lock(lock_path, _BASE)
    commit(repo, "base")
    branches = dict(zip(("one", "two"), _SHAPES[shape], strict=True))
    for name, units in branches.items():
        git(repo, "switch", "-q", "-c", name, "master")
        write_lock(lock_path, units)
        commit(repo, name)

    git(repo, "switch", "-q", "master")
    for name in sorted(branches, reverse=swap):
        merged = git(
            repo, "merge", "-q", "--no-edit", "--no-gpg-sign", name, check=False
        )
        assert merged.returncode == 0, merged.stdout + merged.stderr

    assert not lock_tampered(lock_path)
    expected = dict(_BASE)
    for units in branches.values():
        expected.update({k: v for k, v in units.items() if _BASE.get(k) != v})
    assert read_lock(lock_path) == expected


def test_a_row_moved_to_another_key_by_hand_is_flagged(tmp_path):
    p = tmp_path / "facts.lock"
    write_lock(p, {"CLAUDE.md#Gate": _row("abc", "h")})
    row = p / row_file_name("CLAUDE.md#Gate")
    row.rename(p / row_file_name("CLAUDE.md#Other"))
    assert lock_tampered(p)


def test_a_row_added_by_hand_without_a_checksum_is_flagged(tmp_path):
    p = tmp_path / "facts.lock"
    write_lock(p, {"CLAUDE.md#Gate": _row("abc", "h")})
    (p / row_file_name("CLAUDE.md#New")).write_text(
        '{"key": "CLAUDE.md#New", "row": {"atoms": {}, "verified_sha": "x"}}\n'
    )
    assert lock_tampered(p)


_SINGLE_FILE = '# old header\n{"checksum": "x", "units": {"CLAUDE.md#Gate": %s}}\n'


def test_a_single_file_lock_reads_and_is_replaced_by_the_directory(tmp_path):
    p = tmp_path / "facts.lock"
    p.write_text(_SINGLE_FILE % json.dumps(_row("abc", "h")))
    assert read_lock(p) == {"CLAUDE.md#Gate": _row("abc", "h")}
    assert lock_tampered(p)

    write_lock(p, read_lock(p))

    assert p.is_dir()
    assert read_lock(p) == {"CLAUDE.md#Gate": _row("abc", "h")}
    assert not lock_tampered(p)


def test_lock_at_reads_both_layouts_from_history(tmp_path):
    repo = init_repo(tmp_path)
    empty = commit(repo, "no lock", README="x")
    (repo / "docs").mkdir()
    (repo / LOCK_REL).write_text(_SINGLE_FILE % json.dumps(_row("old", "h")))
    single = commit(repo, "single-file lock")
    write_lock(
        repo / LOCK_REL, {"CLAUDE.md#Gate": _row("new", "h"), _A: _row("new", "a")}
    )
    per_row = commit(repo, "directory lock")

    assert lock_at(repo, single) == {"CLAUDE.md#Gate": _row("old", "h")}
    assert lock_at(repo, per_row) == {
        "CLAUDE.md#Gate": _row("new", "h"),
        _A: _row("new", "a"),
    }
    assert lock_at(repo, empty) is None
