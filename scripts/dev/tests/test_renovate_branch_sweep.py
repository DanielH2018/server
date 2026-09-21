"""`renovate_branch_sweep.py`: the orphan census against fake `gh`/`git`, and `--prune` as opt-in.

Run: uv run pytest scripts/dev/tests/test_renovate_branch_sweep.py
"""

import io
import json
import subprocess

import pytest
from renovate_branch_sweep import DASHBOARD_TITLE, dashboard_body, main, orphans

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


def test_the_dashboard_is_found_by_title_and_absent_reads_as_empty():
    issues = [
        {"title": "other", "body": "approve-branch=renovate/z"},
        {"title": DASHBOARD_TITLE, "body": "hi"},
    ]
    assert dashboard_body(issues) == "hi"
    assert dashboard_body([]) == ""


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
    gh, git, deleted = _tools(
        BRANCHES, ["renovate/a"], [{"title": DASHBOARD_TITLE, "body": DASHBOARD}]
    )
    out = io.StringIO()
    assert main([], gh=gh, git=git, out=out) == 0
    assert "  renovate/b" in out.getvalue()
    assert deleted == []


def test_prune_deletes_exactly_the_orphans():
    gh, git, deleted = _tools(
        BRANCHES, ["renovate/a"], [{"title": DASHBOARD_TITLE, "body": DASHBOARD}]
    )
    assert main(["--prune"], gh=gh, git=git, out=io.StringIO()) == 0
    assert deleted == ["renovate/b"]


def test_an_empty_orphan_set_is_the_healthy_answer():
    gh, git, deleted = _tools(["master"], [], [])
    out = io.StringIO()
    assert main(["--prune"], gh=gh, git=git, out=out) == 0
    assert "no orphan" in out.getvalue()
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
    gh, git, deleted = _tools(["renovate/orphan"], [], [])
    main(["--prune"] if prune else [], gh=gh, git=git, out=io.StringIO())
    assert deleted == (["renovate/orphan"] if prune else [])
