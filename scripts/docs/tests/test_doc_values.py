"""Every value row matches the tree, and the check and the fixer can each go red."""

import pytest

import doc_values
from doc_values import Value, fix, main, stale, staged_paths

_PATTERN = r"a (\d+)-second restart window"


def _row(tmp_path, text, pattern=_PATTERN):
    """A row over `runbook.md` holding `text`, whose source says 180."""
    (tmp_path / "runbook.md").write_text(text)
    return (Value("runbook.md", pattern, "probe.py", lambda source: "180"),)


def test_every_value_row_matches_the_tree():
    assert doc_values.VALUES
    assert stale() == []


def test_a_current_value_is_clean(tmp_path):
    assert (
        stale(_row(tmp_path, "It keeps a 180-second restart window.\n"), tmp_path) == []
    )


def test_a_stale_value_is_flagged_with_both_files(tmp_path):
    rows = _row(tmp_path, "It keeps a 60-second restart window.\n")
    assert stale(rows, tmp_path) == [
        "runbook.md vs probe.py: states 60, the tree holds 180"
    ]


def test_a_reworded_sentence_is_flagged_not_skipped(tmp_path):
    [problem] = stale(_row(tmp_path, "It waits three minutes.\n"), tmp_path)
    assert "nothing matches" in problem


def test_a_pattern_that_matches_twice_is_flagged_and_left_alone(tmp_path):
    text = "a 60-second restart window, then a 60-second restart window.\n"
    rows = _row(tmp_path, text)
    assert fix(rows, tmp_path) == []
    assert (tmp_path / "runbook.md").read_text() == text
    [problem] = stale(rows, tmp_path)
    assert "matches 2 times" in problem


def test_a_pattern_without_a_capture_group_is_rejected(tmp_path):
    rows = _row(tmp_path, "x\n", pattern=r"a \d+-second restart window")
    with pytest.raises(AssertionError, match="exactly one capture group"):
        stale(rows, tmp_path)


def test_the_fixer_rewrites_the_stated_value_and_fails_once(tmp_path, capsys):
    rows = _row(tmp_path, "It keeps a 60-second restart window.\n")
    assert main([], rows, tmp_path) == 1
    assert "rewrote a stated value in: runbook.md" in capsys.readouterr().out
    assert (tmp_path / "runbook.md").read_text() == (
        "It keeps a 180-second restart window.\n"
    )
    assert main([], rows, tmp_path) == 0


def test_a_row_the_fixer_cannot_repair_fails_and_names_the_reason(tmp_path, capsys):
    rows = _row(tmp_path, "It waits three minutes.\n")
    assert main([], rows, tmp_path) == 1
    out = capsys.readouterr().out
    assert "1 row(s) need a hand edit" in out
    assert "runbook.md vs probe.py: nothing matches" in out


def test_staged_paths_reads_one_command_across_its_continuation_lines():
    script = (
        "# git add in a comment\ngit add docs/reference \\\n  weights.json\nexit 0\n"
    )
    assert staged_paths(script) == ["docs/reference", "weights.json"]
