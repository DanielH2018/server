"""Every value row matches the tree, and the check can go red."""

import pytest

import doc_values
from doc_values import Value, mismatches, stale


def test_every_value_row_matches_the_tree():
    assert doc_values.VALUES
    assert stale() == []


def test_a_row_whose_doc_trails_its_source_names_both_files(tmp_path):
    (tmp_path / "runbook.md").write_text("It keeps a 60-second restart window.\n")
    row = Value(
        "runbook.md", r"a (\d+)-second restart window", "probe.py", lambda repo: "180"
    )
    assert stale((row,), tmp_path) == [
        "runbook.md vs probe.py: states 60, the tree holds 180"
    ]


def test_a_current_value_is_clean():
    assert mismatches("retain 14 today", r"retain (\d+)", "14") == []


def test_a_stale_value_is_flagged():
    problems = mismatches("retain 4 today", r"retain (\d+)", "2")
    assert problems == ["states 4, the tree holds 2"]


def test_a_reworded_sentence_is_flagged_not_skipped():
    problems = mismatches("keeps four backups", r"retain (\d+)", "2")
    assert len(problems) == 1
    assert "nothing matches" in problems[0]


def test_every_occurrence_is_checked_not_only_the_first():
    problems = mismatches("retain 2, later retain 4", r"retain (\d+)", "2")
    assert problems == ["states 4, the tree holds 2"]


def test_a_pattern_without_a_capture_group_is_rejected():
    with pytest.raises(AssertionError, match="exactly one capture group"):
        mismatches("retain 2", r"retain \d+", "2")
