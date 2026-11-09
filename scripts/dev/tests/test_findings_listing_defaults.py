"""`list` and `claims` print a count, then the items that need attention (#7897).

`next` and `show` have their default-view tests beside their other tests, in
test_findings_defer.py and test_findings_history.py.

Run: uv run pytest scripts/dev/tests/test_findings_listing_defaults.py
"""

import json

import pytest
from _findings_fakes import Fakes, build_tools, live_worktree, make_issue

from dev.findings import main
from dev.findings_lib.issue_model import claim_comment

LIVE = "worktree-live"
GONE = "worktree-gone"


def _register():
    return [
        make_issue(1, labels=("severity/high", "domain/cicd")),
        make_issue(2, labels=("severity/low", "domain/cicd")),
        make_issue(3, labels=("severity/low", "domain/security")),
    ]


def test_list_opens_on_a_count_by_severity(capsys):
    tools, _ = build_tools(Fakes(issues=_register()))
    assert main(["list"], tools) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "3 open: 1 high, 2 low"
    assert len(out) == 4


def test_list_filters_by_severity_and_domain(capsys):
    tools, _ = build_tools(Fakes(issues=_register()))
    assert main(["list", "--severity", "low", "--domain", "security"], tools) == 0
    out = capsys.readouterr().out
    assert out.startswith("1 open: 1 low")
    assert "#3 " in out and "#2 " not in out


def test_list_limit_prints_how_many_it_left_out(capsys):
    tools, _ = build_tools(Fakes(issues=_register()))
    assert main(["list", "--limit", "1"], tools) == 0
    out = capsys.readouterr().out
    assert out.startswith("3 open: 1 high, 2 low")
    assert "#1 " in out and "#2 " not in out
    assert out.endswith("2 more; raise --limit or drop it\n")


def test_list_without_a_limit_cuts_nothing(capsys):
    tools, _ = build_tools(Fakes(issues=_register()))
    assert main(["list"], tools) == 0
    assert "more; raise --limit" not in capsys.readouterr().out


def test_list_json_applies_the_filters_and_the_limit(capsys):
    tools, _ = build_tools(Fakes(issues=_register()))
    assert main(["list", "--json", "--domain", "cicd", "--limit", "1"], tools) == 0
    captured = capsys.readouterr()
    assert [r["number"] for r in json.loads(captured.out)] == [1]
    assert "1 more; raise --limit or drop it" in captured.err


def _claims():
    return [
        make_issue(1, comments=[claim_comment(LIVE, None, "t")]),
        make_issue(2, comments=[claim_comment(GONE, None, "t")]),
    ]


def test_claims_counts_the_live_ones_and_lists_the_stale_ones(capsys):
    tools, _ = build_tools(Fakes(issues=_claims(), worktree_facts=live_worktree(LIVE)))
    assert main(["claims"], tools) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "1 live, 1 stale"
    assert len(out) == 2 and out[1].startswith("#2 ") and "STALE" in out[1]


def test_claims_all_lists_the_live_ones_too(capsys):
    tools, _ = build_tools(Fakes(issues=_claims(), worktree_facts=live_worktree(LIVE)))
    assert main(["claims", "--all"], tools) == 0
    out = capsys.readouterr().out
    assert "#1 " in out and "#2 " in out


def test_claims_worktree_lists_every_claim_that_branch_holds(capsys):
    tools, _ = build_tools(Fakes(issues=_claims(), worktree_facts=live_worktree(LIVE)))
    assert main(["claims", "--worktree", LIVE], tools) == 0
    out = capsys.readouterr().out
    assert "#1 " in out and "live" in out


def test_claims_json_still_returns_every_claim(capsys):
    tools, _ = build_tools(Fakes(issues=_claims(), worktree_facts=live_worktree(LIVE)))
    assert main(["claims", "--json"], tools) == 0
    assert [r["number"] for r in json.loads(capsys.readouterr().out)] == [1, 2]


def test_a_missing_flag_prints_the_flag_and_one_complete_example(capsys):
    tools, calls = build_tools()
    with pytest.raises(SystemExit) as exit_:
        main(["claim", "5"], tools)
    assert exit_.value.code == 64
    err = capsys.readouterr().err
    assert "the following arguments are required: --worktree" in err
    assert (
        "example: uv run python scripts/dev/findings.py claim 1234 1235 --worktree"
        in err
    )
    assert calls.none()
