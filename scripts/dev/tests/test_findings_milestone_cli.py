"""The `milestone` subcommand, driven through main()."""

import pytest
from _findings_fakes import Fakes, build_tools, facts, live_worktree, make_issue

from dev.findings import main
from dev.findings_lib.issue_model import claim_comment
from dev.findings_lib.milestone import GROUP_DESCRIPTION, description_for, is_project

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


PROJECT = {
    "title": "Backups",
    "number": 9,
    "state": "open",
    "description": description_for(WT, project=True),
}


def test_start_project_posts_the_description_is_project_reads(capsys):
    tools, calls = build_tools(Fakes(api={MILESTONES: []}))
    argv = ["milestone", "start", "--project", "Backups", "--worktree", WT]
    assert main(argv, tools) == 0
    post = next(c for c in calls.gh if c[:3] == ["api", "-X", "POST"])
    assert is_project({"description": post[-1].removeprefix("description=")})
    assert not is_project({"description": description_for(WT)})
    assert capsys.readouterr().out.startswith("project milestone `Backups` open")


def test_status_counts_the_project_issues_apart_from_its_pull_requests(capsys):
    # fact: .claude/skills/coordinate/SKILL.md#Resume
    project = {**PROJECT, "open_issues": 3, "closed_issues": 4, "html_url": "URL"}
    pr = {"pull_request": {"url": "pulls/1"}}
    items = [
        {"state": "open"},
        {"state": "open"},
        {"state": "open", **pr},
        {"state": "closed"},
        {"state": "closed"},
        {"state": "closed", **pr},
        {"state": "closed", **pr},
    ]
    tools, _ = build_tools(
        Fakes(api={MILESTONES: [[project]], "repos/DanielH2018/server/issues": [items]})
    )
    assert main(["milestone", "status", "--worktree", WT], tools) == 0
    assert capsys.readouterr().out == (
        "`Backups` open project: issues 2 open, 2 closed; PRs 1 open, 2 closed  URL\n"
    )


GROUP = {**PROJECT, "description": GROUP_DESCRIPTION, "html_url": "URL"}


def test_group_makes_a_milestone_no_branch_owns_and_files_the_issues(capsys):
    tools, calls = build_tools(
        Fakes(view={1132: make_issue(1132)}, api={MILESTONES: [[]]})
    )
    assert main(["milestone", "group", "Backups", "1132"], tools) == 0
    post, edit = calls.gh
    assert post[:3] == ["api", "-X", "POST"]
    assert post[-1] == f"description={GROUP_DESCRIPTION}"
    assert edit == ["issue", "edit", "1132", "--milestone", "Backups"]
    assert capsys.readouterr().out == "1 added to `Backups`\n"


def test_group_refuses_a_title_a_branch_owns_and_names_its_worktree(capsys):
    tools, calls = _backlog(make_issue(1132))
    assert main(["milestone", "group", "Backups", "1132"], tools) == 3
    assert calls.gh == []
    assert f"pass `--worktree {WT}`" in capsys.readouterr().err


def test_add_and_remove_change_the_group_that_group_names():
    filed = {**make_issue(1140), "milestone": {"title": "Backups", "number": 9}}
    tools, calls = _backlog(make_issue(1132), filed, milestones=[GROUP])
    assert main(["milestone", "add", "1132", "--group", "Backups"], tools) == 0
    assert main(["milestone", "remove", "1140", "--group", "Backups"], tools) == 0
    assert calls.gh == [
        ["issue", "edit", "1132", "--milestone", "Backups"],
        ["issue", "edit", "1140", "--remove-milestone"],
    ]


def test_status_shows_a_group_by_its_title(capsys):
    tools, _ = build_tools(
        Fakes(api={MILESTONES: [[GROUP]], "repos/DanielH2018/server/issues": [[]]})
    )
    assert main(["milestone", "status", "--group", "Backups"], tools) == 0
    assert capsys.readouterr().out.startswith("`Backups` open group: issues 0 open")


def test_close_shuts_a_group_by_its_title():
    tools, calls = _backlog(milestones=[GROUP])
    assert main(["milestone", "close", "--group", "Backups"], tools) == 0
    assert calls.gh == [["api", "-X", "PATCH", f"{MILESTONES}/9", "-f", "state=closed"]]


def test_start_project_adopts_a_project_whose_branch_has_no_worktree(capsys):
    theirs = {**PROJECT, "description": description_for("worktree-b", project=True)}
    tools, calls = build_tools(
        Fakes(api={MILESTONES: [[theirs]]}, worktree_facts=facts())
    )
    argv = ["milestone", "start", "--project", "Backups", "--worktree", WT]
    assert main(argv, tools) == 0
    assert calls.gh[0][:4] == ["api", "-X", "PATCH", f"{MILESTONES}/9"]
    assert capsys.readouterr().out.endswith("adopted the project of `worktree-b`\n")


def test_start_project_refuses_a_project_whose_branch_a_live_worktree_holds(capsys):
    theirs = {**PROJECT, "description": description_for("worktree-b", project=True)}
    tools, calls = build_tools(
        Fakes(api={MILESTONES: [[theirs]]}, worktree_facts=live_worktree("worktree-b"))
    )
    argv = ["milestone", "start", "--project", "Backups", "--worktree", WT]
    assert main(argv, tools) == 3
    assert calls.gh == []
    assert "`worktree-b`, which is live" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["status"],
        ["status", "--worktree", WT, "--group", "Backups"],
        ["start", "Backups", "--group", "Backups"],
        ["group", "Backups", "--worktree", WT],
        ["disown", "--group", "Backups"],
    ],
    ids=["neither", "both", "start-by-group", "group-by-worktree", "disown-by-group"],
)
def test_an_action_refuses_the_naming_flag_it_does_not_take(argv):
    tools, calls = build_tools(Fakes())
    assert main(["milestone", *argv], tools) == 64
    assert calls.gh == calls.gh_json == []


def _backlog(*issues, milestones=(PROJECT,)):
    return build_tools(
        Fakes(
            view={i["number"]: i for i in issues}, api={MILESTONES: [list(milestones)]}
        )
    )


def test_add_files_an_unclaimed_issue_and_refuses_one_another_milestone_holds(capsys):
    free = make_issue(1132)
    taken = {**make_issue(1140), "milestone": {"title": "Release 1.0", "number": 3}}
    tools, calls = _backlog(free, taken)
    assert main(["milestone", "add", "1132", "1140", "--worktree", WT], tools) == 3
    assert calls.gh == [["issue", "edit", "1132", "--milestone", "Backups"]]
    captured = capsys.readouterr()
    assert captured.out == "1 added to `Backups`, 1 refused\n"
    assert "#1140 refused: in milestone `Release 1.0`" in captured.err


def test_add_is_refused_on_a_branch_whose_milestone_is_not_a_project(capsys):
    session = {**PROJECT, "description": description_for(WT)}
    tools, calls = _backlog(make_issue(1132), milestones=[session])
    assert main(["milestone", "add", "1132", "--worktree", WT], tools) == 3
    assert calls.gh == []
    assert "has no open project milestone" in capsys.readouterr().err


def test_remove_takes_a_backlog_issue_out_and_refuses_a_claimed_one(capsys):
    held = {"title": "Backups", "number": 9}
    idle = {**make_issue(1132), "milestone": held}
    claimed = {
        **make_issue(1140, comments=[claim_comment("worktree-b", None, "t")]),
        "milestone": held,
    }
    tools, calls = _backlog(idle, claimed)
    assert main(["milestone", "remove", "1132", "1140", "--worktree", WT], tools) == 3
    assert calls.gh == [["issue", "edit", "1132", "--remove-milestone"]]
    assert "#1140 refused: claimed by `worktree-b`" in capsys.readouterr().err
