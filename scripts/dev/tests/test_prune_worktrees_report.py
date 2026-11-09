"""The long report counts the worktrees it keeps and lists the ones that need a decision (#7897).

Run: uv run pytest scripts/dev/tests/test_prune_worktrees_report.py
"""

from lib.worktrees import REMOVABLE, Worktree
from prune_worktrees import VANISHED, survey_report


def _tree(name):
    return Worktree(
        path=f"/repo/.claude/worktrees/{name}",
        head="abc",
        branch=f"b-{name}",
        locked=False,
    )


SURVEYED = [
    (REMOVABLE, _tree("merged"), "merged"),
    (VANISHED, _tree("gone"), "directory gone"),
    ("keep", _tree("busy"), "has uncommitted work"),
    ("keep", _tree("locked"), "locked by a live session"),
]


def test_the_long_report_lists_removable_and_vanished_and_counts_the_kept():
    lines, removable, vanished = survey_report(SURVEYED, show_all=False)
    out = "\n".join(lines)
    assert "merged" in out and "directory gone" in out
    assert "has uncommitted work" not in out
    assert lines[-1] == "2 worktree(s) kept; --all lists them"
    assert [t.branch for t in removable] == ["b-merged"] and vanished == 1


def test_all_lists_the_kept_worktrees_too():
    lines, _, _ = survey_report(SURVEYED, show_all=True)
    out = "\n".join(lines)
    assert "has uncommitted work" in out and "locked by a live session" in out
    assert "kept; --all lists them" not in out
