"""facts.lock: written by the tool, checksummed against a hand edit, and checked against the tree."""

import subprocess

import pytest

from facts.lock import (
    FINDING_KINDS,
    LOCK_REL,
    _PYTHON,
    Finding,
    build_repo_edb,
    check_lock,
    forget_units,
    lock_tampered,
    read_lock,
    verify_units,
    write_lock,
)
from facts.relations import derive, status_of

DOC = """# Root

## Gate
`{repo}m.py:LIMIT` bounds it, ENFORCED by `t/test_m.py::test_limit`.

## Style
Write commit messages that explain why.
"""


def _repo(tmp_path):
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
    }
    subprocess.run(
        ["git", "init", "-q", "-b", "master", str(tmp_path)], check=True, env=env
    )
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "m.py").write_text("LIMIT = 85\n")
    (tmp_path / "t" / "test_m.py").write_text(
        "def test_limit():\n    # fact: CLAUDE.md#Gate\n    assert True\n"
    )
    (tmp_path / "CLAUDE.md").write_text(DOC.format(repo="t/"))
    (tmp_path / "docs").mkdir()
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, env=env)
    return tmp_path


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
    p.write_text(p.read_text().replace('"h"', '"hh"').replace(": h\n", ": hh\n"))
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
    p.write_text(p.read_text().replace("abc1234", "zzz9999"))
    assert [f.kind for f in check_lock(repo, p)] == ["lock-tampered"]


def test_repo_edb_gives_statuses(tmp_path):
    repo = _repo(tmp_path)
    verify_units(repo, repo / LOCK_REL, ["CLAUDE.md#Gate"], "abc1234")
    edb = build_repo_edb(repo, read_lock(repo / LOCK_REL))
    idb = derive(edb)
    assert status_of(edb, idb, "CLAUDE.md#Gate") == "IN"
    assert status_of(edb, idb, "CLAUDE.md#Style") == "CONVENTION"
    assert ("t/test_m.py::test_limit", "CLAUDE.md#Gate") in edb.backref
    assert not idb.one_way


def test_unverified_section_with_citations_is_unverified(tmp_path):
    repo = _repo(tmp_path)
    edb = build_repo_edb(repo, {})
    assert status_of(edb, derive(edb), "CLAUDE.md#Gate") == "UNVERIFIED"


def test_new_citation_on_a_locked_unit_is_flagged(tmp_path):
    """The clean twin is ``test_verify_then_check_is_clean``."""
    repo = _repo(tmp_path)
    verify_units(repo, repo / LOCK_REL, ["CLAUDE.md#Gate"], "abc1234")
    (repo / "t" / "other.py").write_text("CAP = 3\n")
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
        }
    )
