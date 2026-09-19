"""Each lint rule as a clean/flagged pair over a one-file repo."""

import subprocess

from facts.lint import RULES, WARN_RULES, changed_units, lint_sections
from facts.lock import LOCK_REL, write_lock


def _repo(tmp_path, doc):
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
    }
    subprocess.run(
        ["git", "init", "-q", "-b", "master", str(tmp_path)], check=True, env=env
    )
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f.txt").write_text("x\n")
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "m.py").write_text("LIMIT = 85\n# DECIDED: keep it\n")
    (tmp_path / "t" / "test_m.py").write_text("def test_a():\n    assert True\n")
    (tmp_path / "CLAUDE.md").write_text(doc)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, env=env)
    return tmp_path, env


def _rules(tmp_path, doc):
    repo, _ = _repo(tmp_path, doc)
    return {(f.unit, f.rule) for f in lint_sections(repo, None)}


def test_clean_section_has_no_findings(tmp_path):
    assert (
        _rules(
            tmp_path, "## A\n`t/m.py:LIMIT` and `d/` and `t/m.py:DECIDED: keep it`.\n"
        )
        == set()
    )


def test_out_of_tree_citation_is_ignored(tmp_path):
    """A slashed token that names no repo root is prose: neither an error nor a warning."""
    assert (
        _rules(
            tmp_path, "## A\nrebase onto `origin/master`, then `defaults/main.yml`\n"
        )
        == set()
    )


def test_file_line_is_flagged(tmp_path):
    assert ("CLAUDE.md#A", "rejected-form") in _rules(
        tmp_path, "## A\nsee `t/m.py:1`\n"
    )


def test_dir_without_slash_is_flagged(tmp_path):
    repo, _ = _repo(tmp_path, "## A\nsee `d/sub`\n")
    (repo / "d" / "sub").mkdir()
    assert ("CLAUDE.md#A", "dir-without-slash") in {
        (f.unit, f.rule) for f in lint_sections(repo, None)
    }


def test_dir_with_slash_is_clean(tmp_path):
    repo, _ = _repo(tmp_path, "## A\nsee `d/sub/`\n")
    (repo / "d" / "sub").mkdir()
    assert not lint_sections(repo, None)


def test_unresolved_atom_is_flagged(tmp_path):
    assert ("CLAUDE.md#A", "unresolved-atom") in _rules(
        tmp_path, "## A\n`t/m.py:NOPE`\n"
    )


def test_ambiguous_marker_is_flagged(tmp_path):
    repo, _ = _repo(tmp_path, "## A\n`t/m.py:DECIDED: keep`\n")
    (repo / "t" / "m.py").write_text("# DECIDED: keep it\n# DECIDED: keep that\n")
    assert ("CLAUDE.md#A", "ambiguous-marker") in {
        (f.unit, f.rule) for f in lint_sections(repo, None)
    }


def test_one_way_test_is_a_warning(tmp_path):
    found = [
        f
        for f in lint_sections(
            _repo(tmp_path, "## A\n`t/test_m.py::test_a`\n")[0], None
        )
    ]
    assert [(f.rule, f.warn) for f in found] == [("one-way-test", True)]


def test_backreferenced_test_is_clean(tmp_path):
    repo, _ = _repo(tmp_path, "## A\n`t/test_m.py::test_a`\n")
    (repo / "t" / "test_m.py").write_text(
        "def test_a():\n    # fact: CLAUDE.md#A\n    assert True\n"
    )
    assert not lint_sections(repo, None)


def test_count_as_fact_is_a_warning(tmp_path):
    found = lint_sections(
        _repo(tmp_path, "## A\nThere are 13 entries here.\n")[0], None
    )
    assert [(f.rule, f.warn) for f in found] == [("count-as-fact", True)]


def test_date_as_verification_is_flagged(tmp_path):
    found = lint_sections(
        _repo(tmp_path, "## A\nVerified against the tree 2026-09-17.\n")[0], None
    )
    assert [(f.rule, f.warn) for f in found] == [("date-as-verification", False)]


def test_only_named_units_are_linted(tmp_path):
    repo, _ = _repo(tmp_path, "## A\n`t/m.py:1`\n\n## B\n`t/m.py:2`\n")
    assert {f.unit for f in lint_sections(repo, {"CLAUDE.md#B"})} == {"CLAUDE.md#B"}


def test_changed_units_names_only_edited_sections(tmp_path):
    repo, env = _repo(tmp_path, "## A\none\n\n## B\ntwo\n")
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@x",
            "commit",
            "-q",
            "-m",
            "i",
            "--no-gpg-sign",
        ],
        cwd=repo,
        check=True,
        env=env,
    )
    (repo / "CLAUDE.md").write_text("## A\none\n\n## B\ntwo changed\n")
    assert changed_units(repo, "HEAD") == {"CLAUDE.md#B"}


def test_rule_census():
    assert RULES == frozenset(
        {
            "rejected-form",
            "dir-without-slash",
            "unresolved-atom",
            "ambiguous-marker",
            "one-way-test",
            "lock-tampered",
            "count-as-fact",
            "date-as-verification",
        }
    )
    assert WARN_RULES == frozenset({"count-as-fact", "one-way-test"})


def test_hand_edited_lock_is_flagged(tmp_path):
    """The clean side (no lock file) is already covered by every other test above."""
    repo, _ = _repo(tmp_path, "## A\n`t/m.py:LIMIT`\n")
    write_lock(
        repo / LOCK_REL,
        {"CLAUDE.md#A": {"verified_sha": "abc", "atoms": {"t/m.py:LIMIT": "h"}}},
    )
    p = repo / LOCK_REL
    p.write_text(p.read_text().replace('"h"', '"hh"'))
    found = {(f.unit, f.rule): f.warn for f in lint_sections(repo, None)}
    assert found[("", "lock-tampered")] is False
