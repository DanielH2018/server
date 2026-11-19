"""The `milestone` subcommand, driven through main()."""

from _findings_fakes import Fakes, build_tools, make_issue

from dev.findings import main
from dev.findings_lib.issue_model import claim_comment
from dev.findings_lib.milestone import description_for

WT = "worktree-a"
MILESTONES = "repos/DanielH2018/server/milestones"


def test_start_files_the_branch_claims_and_open_pr_under_the_new_milestone():
    mine = make_issue(1132, comments=[claim_comment(WT, None, "t")])
    other = make_issue(1140)
    tools, calls = build_tools(
        Fakes(issues=[mine, other], prs=[{"number": 77}], api={MILESTONES: []})
    )
    assert main(["milestone", "start", "Focus", "--worktree", WT], tools) == 0
    assert any(c[:3] == ["api", "-X", "POST"] for c in calls.gh)
    assert ["issue", "edit", "1132", "--milestone", "Focus"] in calls.gh
    assert ["pr", "edit", "77", "--milestone", "Focus"] in calls.gh
    assert not any(c[:3] == ["issue", "edit", "1140"] for c in calls.gh)


def test_status_says_how_to_start_when_the_branch_has_no_milestone(capsys):
    tools, calls = build_tools(Fakes(api={MILESTONES: []}))
    assert main(["milestone", "status", "--worktree", WT], tools) == 0
    assert "has no milestone" in capsys.readouterr().out
    assert calls.gh == []


def test_close_shuts_the_branch_open_milestone():
    focus = {
        "title": "Focus",
        "number": 9,
        "state": "open",
        "description": description_for(WT),
    }
    tools, calls = build_tools(Fakes(api={MILESTONES: [[focus]]}))
    assert main(["milestone", "close", "--worktree", WT], tools) == 0
    assert calls.gh == [["api", "-X", "PATCH", f"{MILESTONES}/9", "-f", "state=closed"]]
