"""`renovate_branch_sweep.py`: the orphan census against fake `gh`/`git`, and `--prune` as opt-in.

Run: uv run pytest scripts/dev/tests/test_renovate_branch_sweep.py
"""

import io
import json
import subprocess

import pytest
from renovate_branch_sweep import (
    DASHBOARD_TITLE,
    NoDashboard,
    dashboard_body,
    main,
    orphans,
)

BRANCHES = [
    "master",
    "renovate/a",
    "renovate/b",
    "renovate/c",
    "renovate/d",
    "worktree-x",
]
DASHBOARD = (
    "## Pending Status Checks\n\n"
    " - [ ] <!-- approve-branch=renovate/c -->x\n"
    " - [ ] <!-- other-verb-branch=renovate/d -->y\n"
)


def test_a_branch_with_an_open_pr_or_a_dashboard_entry_is_clean():
    assert orphans(BRANCHES, ["renovate/a", "renovate/b"], DASHBOARD) == []


def test_a_branch_nothing_speaks_for_is_flagged():
    assert orphans(BRANCHES, ["renovate/a"], DASHBOARD) == ["renovate/b"]


def test_any_dashboard_verb_counts_not_only_the_known_three():
    # #1629: a section marker with an unmatched verb made every branch under it an orphan.
    assert orphans(["renovate/d"], [], DASHBOARD) == []


def test_a_non_renovate_branch_is_never_an_orphan():
    assert orphans(["master", "worktree-x"], [], "") == []


RENOVATE = {"login": "app/renovate"}
DASHBOARD_ISSUE = {"title": DASHBOARD_TITLE, "body": DASHBOARD, "author": RENOVATE}


def test_the_dashboard_is_matched_by_title_and_renovate_author():
    look_alike = {
        "title": DASHBOARD_TITLE,
        "body": "fake",
        "author": {"login": "daniel"},
    }
    assert dashboard_body([look_alike, DASHBOARD_ISSUE]) == DASHBOARD


def test_an_absent_dashboard_is_refused_not_read_as_empty():
    # #1629 through a different door: an empty body would orphan every branch it names.
    with pytest.raises(NoDashboard):
        dashboard_body([])
    with pytest.raises(NoDashboard):
        dashboard_body(
            [{"title": DASHBOARD_TITLE, "body": "x", "author": {"login": "me"}}]
        )


def _tools(branches, heads, issues):
    deleted: list[str] = []

    def gh(*args, **kwargs):
        if args[0] == "api":
            return subprocess.CompletedProcess(args, 0, "\n".join(branches), "")
        if args[:2] == ("pr", "list"):
            return subprocess.CompletedProcess(args, 0, "\n".join(heads), "")
        if args[:2] == ("issue", "list"):
            return subprocess.CompletedProcess(args, 0, json.dumps(issues), "")
        raise AssertionError(f"unexpected gh call {args}")

    def git(*args, **kwargs):
        assert args[:3] == ("push", "origin", "--delete")
        deleted.append(args[3])
        return subprocess.CompletedProcess(args, 0, "", "")

    return gh, git, deleted


def test_a_report_names_the_orphans_and_deletes_nothing():
    gh, git, deleted = _tools(BRANCHES, ["renovate/a"], [DASHBOARD_ISSUE])
    out = io.StringIO()
    assert main([], gh=gh, git=git, out=out) == 0
    assert "  renovate/b" in out.getvalue()
    assert deleted == []


def test_prune_deletes_exactly_the_orphans():
    gh, git, deleted = _tools(BRANCHES, ["renovate/a"], [DASHBOARD_ISSUE])
    assert main(["--prune"], gh=gh, git=git, out=io.StringIO()) == 0
    assert deleted == ["renovate/b"]


def test_an_empty_orphan_set_is_the_healthy_answer():
    gh, git, deleted = _tools(["master"], [], [DASHBOARD_ISSUE])
    out = io.StringIO()
    assert main(["--prune"], gh=gh, git=git, out=out) == 0
    assert "no orphan" in out.getvalue()
    assert deleted == []


def test_a_missing_dashboard_stops_the_sweep_before_any_delete():
    gh, git, deleted = _tools(["renovate/orphan"], [], [])
    out = io.StringIO()
    assert main(["--prune"], gh=gh, git=git, out=out) == 2
    assert "Dependency Dashboard" in out.getvalue()
    assert deleted == []


def test_a_failed_gh_read_is_reported_not_graded():
    def gh(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args, "", "not logged in")

    def git(*args, **kwargs):
        raise AssertionError(f"git must not run when the census failed: {args}")

    out = io.StringIO()
    assert main([], gh=gh, git=git, out=out) == 2
    assert "not logged in" in out.getvalue()


@pytest.mark.parametrize("prune", [False, True])
def test_the_prune_flag_is_the_only_write_path(prune):
    gh, git, deleted = _tools(["renovate/orphan"], [], [DASHBOARD_ISSUE])
    main(["--prune"] if prune else [], gh=gh, git=git, out=io.StringIO())
    assert deleted == (["renovate/orphan"] if prune else [])
