"""`history` and `show`: past findings by topic or file, with how each one closed.

The fake `issue list` ignores `--search` and answers every hit it holds, so the tests that
care what gh was asked assert on the argv rather than on the filtering gh would do.
"""

import json

from _findings_fakes import Fakes, build_tools, foreign_comment, make_issue

from dev.findings import main
from dev.findings_lib.cli import HISTORY_LIMIT


def _closed(number, *, reason, labels=(), comments=(), title="t", **fields):
    issue = make_issue(
        number, state="CLOSED", labels=labels, comments=comments, title=title
    )
    issue.update(stateReason=reason, closedAt="2026-10-03T19:50:51Z", **fields)
    return issue


def _rows(capsys, argv, issues):
    tools, calls = build_tools(Fakes(issues=issues))
    assert main([*argv, "--json"], tools) == 0
    return json.loads(capsys.readouterr().out), calls


def test_history_searches_every_state_through_gh_search(capsys):
    _, calls = _rows(capsys, ["history", "bootstrap", "guard"], [])
    argv = calls.gh_json[0]
    assert argv[argv.index("--state") + 1] == "all"
    assert argv[argv.index("--search") + 1] == "bootstrap guard"
    assert argv[argv.index("--limit") + 1] == str(HISTORY_LIMIT)
    fields = argv[argv.index("--json") + 1].split(",")
    assert {"stateReason", "closedByPullRequestsReferences", "closedAt"} <= set(fields)


def test_a_refuted_finding_prints_its_reason_and_close_date(capsys):
    refuted = _closed(
        3284,
        reason="NOT_PLANNED",
        labels=("refuted",),
        comments=["Refuted: the mutation run disproved it.\nSecond line."],
        title="Narrow the static bootstrap guard",
    )
    tools, _ = build_tools(Fakes(issues=[refuted]))
    assert main(["history", "bootstrap"], tools) == 0
    out = capsys.readouterr().out
    assert "#3284  refuted     2026-10-03  Narrow the static bootstrap guard" in out
    assert "reason: the mutation run disproved it." in out


def test_a_hand_close_as_not_planned_is_not_reported_as_refuted(capsys):
    """The rejecting half: gh records refuted and accepted both as NOT_PLANNED, so the
    outcome must come from the label, and a close with no label says what it was."""
    rows, _ = _rows(capsys, ["history", "x"], [_closed(9, reason="NOT_PLANNED")])
    assert rows[0]["outcome"] == "not planned"
    assert rows[0]["reason"] is None


def test_an_open_and_a_reopened_finding_read_as_open(capsys):
    reopened = make_issue(8)
    reopened["stateReason"] = "REOPENED"
    rows, _ = _rows(capsys, ["history", "x"], [make_issue(7), reopened])
    assert [r["outcome"] for r in rows] == ["open", "open"]
    assert [r["closed"] for r in rows] == [None, None]


def test_the_closing_pr_comes_from_a_keyword_or_from_close_fixed(capsys):
    by_keyword = _closed(
        1,
        reason="COMPLETED",
        closedByPullRequestsReferences=[
            {
                "number": 3679,
                "repository": {"name": "server", "owner": {"login": "DanielH2018"}},
            },
            {
                "number": 12,
                "repository": {"name": "dotfiles", "owner": {"login": "DanielH2018"}},
            },
        ],
    )
    by_close_fixed = _closed(2, reason="COMPLETED", comments=["Fixed by PR #3311."])
    rows, _ = _rows(capsys, ["history", "x"], [by_keyword, by_close_fixed])
    assert [(r["outcome"], r["prs"]) for r in rows] == [
        ("fixed", [3679]),
        ("fixed", [3311]),
    ]


def test_a_fixed_by_line_from_outside_the_repo_names_no_pr(capsys):
    """The rejecting half: this repo is public, so any account can write the line."""
    issue = _closed(
        2, reason="COMPLETED", comments=[foreign_comment("Fixed by PR #1.")]
    )
    rows, _ = _rows(capsys, ["history", "x"], [issue])
    assert rows[0]["prs"] == []


def test_history_file_keeps_only_findings_whose_body_cites_the_path(capsys):
    cites = make_issue(1)
    cites["body"] = "See `scripts/dev/findings_lib/gh_calls.py:88` for the cap."
    under_dir = make_issue(2)
    under_dir["body"] = "And scripts/dev/findings_lib/plans.py too."
    neighbour = make_issue(3)
    neighbour["body"] = "Only scripts/dev/findings.py is cited here."
    issues = [cites, under_dir, neighbour]

    rows, calls = _rows(
        capsys, ["history", "--file", "scripts/dev/findings_lib/gh_calls.py:5"], issues
    )
    assert [r["number"] for r in rows] == [1]
    argv = calls.gh_json[0]
    assert argv[argv.index("--search") + 1] == '"scripts/dev/findings_lib/gh_calls.py"'

    rows, _ = _rows(capsys, ["history", "--file", "scripts/dev/findings_lib/"], issues)
    assert [r["number"] for r in rows] == [1, 2]


def test_history_warns_when_the_search_fills_its_limit(capsys):
    tools, _ = build_tools(Fakes(issues=[make_issue(n) for n in (1, 2)]))
    assert main(["history", "x", "--limit", "2"], tools) == 0
    assert "warning: 2 search hits" in capsys.readouterr().err


def test_history_does_not_warn_below_its_limit(capsys):
    tools, _ = build_tools(Fakes(issues=[make_issue(1)]))
    assert main(["history", "x", "--limit", "2"], tools) == 0
    assert "warning" not in capsys.readouterr().err


def test_history_with_nothing_to_search_for_is_a_usage_error(capsys):
    tools, calls = build_tools()
    assert main(["history"], tools) == 2
    assert calls.none()


def test_show_prints_the_body_and_the_whole_thread_naming_outsiders(capsys):
    issue = _closed(
        5,
        reason="NOT_PLANNED",
        labels=("accepted",),
        comments=[
            foreign_comment("drive-by"),
            "Accepted: true, and cheaper to live with.\nThe long form is here.",
        ],
    )
    tools, _ = build_tools(Fakes(view=issue))
    assert main(["show", "5"], tools) == 0
    out = capsys.readouterr().out
    assert "accepted" in out and "details" in out
    assert "The long form is here." in out
    assert "drive-by-account (not the operator)" in out


def test_show_refuses_an_issue_outside_the_register(capsys):
    issue = make_issue(5)
    issue["labels"] = []
    tools, _ = build_tools(Fakes(view=issue))
    assert main(["show", "5"], tools) == 3
    assert "not a `claude` finding" in capsys.readouterr().err
