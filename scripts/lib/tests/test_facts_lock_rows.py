"""facts.lock rows that record nothing, and the first citation a commit gives a section.

A row whose section cites nothing grades nothing, so the lock reports it as `empty-row`. A
section the commit gives its first citation is recorded by the reverify hook, the same as a
citation added to a verified section, but a section with a row keeps the #2817 contract.
"""

from lib.facts.lint import changed_units, first_cited_units
from lib.facts.lock import (
    LOCK_REL,
    check_lock,
    read_lock,
    reverify_benign,
    verify_units,
    write_lock,
)
from lib.git_testing import commit, init_repo

_CODE = {"t/m.py": "LIMIT = 85\nCAP = 3\n"}


def _repo(tmp_path, doc):
    repo = init_repo(tmp_path)
    commit(repo, "base", **{**_CODE, "CLAUDE.md": doc})
    return repo


def test_a_row_whose_section_cites_nothing_is_flagged(tmp_path):
    repo = _repo(tmp_path, "## Style\nWrite why, not what.\n")
    write_lock(repo / LOCK_REL, {"CLAUDE.md#Style": {"verified_sha": "a", "atoms": {}}})
    assert [(f.unit, f.kind) for f in check_lock(repo, repo / LOCK_REL)] == [
        ("CLAUDE.md#Style", "empty-row")
    ]


def test_verify_writes_no_row_for_a_section_that_cites_nothing(tmp_path):
    repo = _repo(tmp_path, "## Style\nWrite why, not what.\n")
    write_lock(repo / LOCK_REL, {"CLAUDE.md#Style": {"verified_sha": "a", "atoms": {}}})
    lock, _ = verify_units(repo, repo / LOCK_REL, ["CLAUDE.md#Style"], "abc1234")
    assert lock == {} and read_lock(repo / LOCK_REL) == {}


def test_reverify_drops_the_row_of_a_section_whose_last_citation_went(tmp_path):
    repo = _repo(tmp_path, "## Gate\n`t/m.py:LIMIT` bounds it.\n")
    verify_units(repo, repo / LOCK_REL, ["CLAUDE.md#Gate"], "abc1234")
    (repo / "CLAUDE.md").write_text("## Gate\nA limit bounds it.\n")

    done, blocking = reverify_benign(
        repo, repo / LOCK_REL, changed_units(repo, "HEAD"), "def5678"
    )

    assert (done, blocking) == (["CLAUDE.md#Gate"], [])
    assert read_lock(repo / LOCK_REL) == {}
    assert check_lock(repo, repo / LOCK_REL) == []


def _reverify(repo):
    changed = changed_units(repo, "HEAD")
    first = first_cited_units(repo, "HEAD", changed)
    return reverify_benign(repo, repo / LOCK_REL, changed, "def5678", None, first)


def test_a_section_given_its_first_citation_is_recorded(tmp_path):
    repo = _repo(tmp_path, "## Gate\nA limit bounds it.\n")
    (repo / "CLAUDE.md").write_text("## Gate\n`t/m.py:LIMIT` bounds it.\n")

    assert _reverify(repo) == (["CLAUDE.md#Gate"], [])
    row = read_lock(repo / LOCK_REL)["CLAUDE.md#Gate"]
    assert set(row["atoms"]) == {"t/m.py:LIMIT"} and row["verified_sha"] == "def5678"


def test_a_new_section_with_a_citation_is_recorded(tmp_path):
    repo = _repo(tmp_path, "## Style\nWrite why.\n")
    (repo / "CLAUDE.md").write_text(
        "## Style\nWrite why.\n\n## Gate\n`t/m.py:LIMIT` bounds it.\n"
    )
    assert _reverify(repo) == (["CLAUDE.md#Gate"], [])


def test_a_backlog_section_edited_without_a_first_citation_stays_unrecorded(tmp_path):
    """It already cited an atom before the commit, so this edit is not when that was written."""
    repo = _repo(tmp_path, "## Gate\n`t/m.py:LIMIT` bounds it.\n")
    (repo / "CLAUDE.md").write_text(
        "## Gate\n`t/m.py:LIMIT` bounds it, and `t/m.py:CAP` caps it.\n"
    )
    assert _reverify(repo) == ([], [])
    assert read_lock(repo / LOCK_REL) == {}


def _rename_gate(repo, limit):
    """Verify Gate and commit, then rename its heading and set ``LIMIT`` in the working tree."""
    verify_units(repo, repo / LOCK_REL, ["CLAUDE.md#Gate"], "abc1234")
    commit(repo, "verify")
    (repo / "t" / "m.py").write_text(f"LIMIT = {limit}\nCAP = 3\n")
    (repo / "CLAUDE.md").write_text("## The gate\n`t/m.py:LIMIT` bounds it.\n")


def test_a_renamed_heading_whose_atom_moved_is_not_recorded(tmp_path):
    """The rename makes a key with no row; recording it would launder the move (#2817)."""
    repo = _repo(tmp_path, "## Gate\n`t/m.py:LIMIT` bounds it.\n")
    _rename_gate(repo, 86)
    done, blocking = _reverify(repo)
    assert done == []
    assert [(f.unit, f.kind) for f in blocking] == [("CLAUDE.md#Gate", "section-gone")]
    assert set(read_lock(repo / LOCK_REL)) == {"CLAUDE.md#Gate"}


def test_a_renamed_heading_whose_atoms_held_is_recorded(tmp_path):
    repo = _repo(tmp_path, "## Gate\n`t/m.py:LIMIT` bounds it.\n")
    _rename_gate(repo, 85)
    assert _reverify(repo)[0] == ["CLAUDE.md#The gate"]
