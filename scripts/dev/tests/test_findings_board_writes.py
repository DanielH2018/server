"""`findings.main` syncs the Project board for every issue a subcommand wrote, and only those."""

from _findings_fakes import Fakes, build_tools, live_worktree, make_issue

from dev.findings import main

WT = "worktree-issue-1132"


def test_claim_syncs_the_claimed_issue():
    issue = make_issue(1132)
    tools, calls = build_tools(
        Fakes(issues=[issue], view=issue, worktree_facts=live_worktree(WT))
    )

    assert main(["claim", "1132", "--worktree", WT], tools) == 0

    assert calls.board == [([1132], None)]


def test_open_syncs_the_issue_it_created(tmp_path):
    body = tmp_path / "b.md"
    body.write_text("B")
    tools, calls = build_tools(Fakes())

    argv = ["open", "--title", "T", "--body-file", str(body)]
    assert main([*argv, "--severity", "low", "--kind", "gap"], tools) == 0

    assert calls.board == [([42], None)]


def test_a_dry_run_and_a_read_sync_nothing():
    issue = make_issue(1132)
    tools, calls = build_tools(
        Fakes(issues=[issue], view=issue, worktree_facts=live_worktree(WT))
    )

    main(["claim", "1132", "--worktree", WT, "--dry-run"], tools)
    main(["list"], tools)

    assert calls.board == []


def test_another_repos_register_has_no_board():
    issue = make_issue(1132)
    tools, calls = build_tools(Fakes(issues=[issue], view=issue))

    assert main(["defer", "1132", "--until", "2099-01-15", "--repo", "o/r"], tools) == 0

    assert calls.board == []


def test_a_failed_board_write_warns_once_and_keeps_the_exit_code(capsys):
    issue = make_issue(1132)
    tools, _ = build_tools(
        Fakes(
            issues=[issue],
            view=issue,
            worktree_facts=live_worktree(WT),
            board={1132: ("failed", "token lacks project scope")},
        )
    )

    assert main(["claim", "1132", "--worktree", WT], tools) == 0

    warnings = [ln for ln in capsys.readouterr().err.splitlines() if "board" in ln]
    assert warnings == [
        "warning: board not updated for #1132: token lacks project scope; "
        "repair with `findings.py sync-board 1132`"
    ]


def test_sync_board_covers_every_open_issue_and_exits_1_on_a_failure(capsys):
    tools, calls = build_tools(
        Fakes(
            issues=[make_issue(1), make_issue(2), make_issue(3)],
            board={
                1: ("updated", "Status Todo -> In Progress"),
                3: ("failed", "not an issue"),
            },
        )
    )

    assert main(["sync-board"], tools) == 1

    assert calls.board == [([1, 2, 3], None)]
    assert capsys.readouterr().out.splitlines() == [
        "3 issues: 1 updated, 1 unchanged, 1 failed",
        "#1 Status Todo -> In Progress",
        "#3 failed: not an issue",
    ]
