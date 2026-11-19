"""The `findings` source cc-wait reads: `findings.py new-issues`, answered from REST fakes.

Run: uv run pytest scripts/dev/tests/test_findings_new_issue_probe.py
"""

import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from _findings_fakes import OPERATOR, Fakes, build_tools, facts, live_worktree

from dev.findings import main
from dev.findings_lib.boundaries import REGISTER_CHECKOUTS
from dev.findings_lib.issue_model import claim_comment
from dev.findings_lib import new_issue_probe

TODAY = date(2026, 11, 14)
REG = "repos/DanielH2018/server"
WT = "worktree-busy"


def _rest_issue(
    number, *, labels=(), comments=0, blocked_by=0, body="details", pr=False
):
    """One entry of REST's open-issue list, the shape `gh api repos/R/issues` returns."""
    issue = {
        "number": number,
        "title": f"finding {number}",
        "state": "open",
        "labels": [{"name": n} for n in ("claude", *labels)],
        "body": body,
        "user": {"login": OPERATOR},
        "html_url": f"https://github.com/DanielH2018/server/issues/{number}",
        "created_at": "2026-11-14T10:00:00Z",
        "comments": comments,
        "issue_dependencies_summary": {"blocked_by": blocked_by},
    }
    if pr:
        issue["pull_request"] = {"url": "…"}
    return issue


def _tools(issues, *, worktrees=None, **api):
    """Fakes answering the open-issue read with ``issues`` and each other REST path from ``api``."""
    answers = {f"{REG}/issues": issues, f"{REG}/pulls": [], **api}
    return build_tools(Fakes(api=answers, worktree_facts=worktrees or facts()))


def test_describe_forgets_the_last_baseline(tmp_path):
    baseline = tmp_path / "b.json"
    baseline.write_text('{"after": 9}')
    assert new_issue_probe.describe(baseline)["interval_s"] == 300
    assert not baseline.exists()


def test_the_declared_states_keep_cc_wait_s_contract():
    """cc-wait refuses a source whose every terminal state exits 0, or that claims 2 or 75."""
    codes = set(new_issue_probe.describe(None)["terminal"].values())
    assert 0 in codes
    assert any(code != 0 for code in codes)
    assert not {2, 75} & codes


def test_gh_with_no_login_ends_the_wait_gh_unauthenticated(capsys):
    error = subprocess.CalledProcessError(4, ["gh"], stderr="gh auth login required")
    tools, _ = build_tools(Fakes(json_errors={f"api {REG}/issues": error}))
    assert main(["new-issues", "--after", "5"], tools) == 0
    reading = json.loads(capsys.readouterr().out)
    assert reading == {
        "state": "gh-unauthenticated",
        "detail": "gh auth login required",
    }


def test_a_wait_with_no_after_ends_only_on_an_issue_filed_after_it_began(tmp_path):
    baseline = tmp_path / "b.json"
    tools, _ = _tools([_rest_issue(7), _rest_issue(5)])
    first = new_issue_probe.read(tools, None, baseline, TODAY)
    assert first == {"state": "none", "detail": "no new free issue above #7"}

    tools, _ = _tools([_rest_issue(8), _rest_issue(7), _rest_issue(5)])
    second = new_issue_probe.read(tools, None, baseline, TODAY)
    assert second["state"] == "new"
    assert [i["number"] for i in json.loads(second["detail"])["issues"]] == [8]


def test_a_free_issue_above_after_ends_the_wait_naming_what_a_launch_needs(tmp_path):
    body = "`scripts/dev/fanout.py` drops the batch"
    issues = [
        _rest_issue(7, labels=("severity/high", "no-vetted-remediation"), body=body),
        _rest_issue(5),
    ]
    tools, _ = _tools(issues)
    reading = new_issue_probe.read(tools, 5, tmp_path / "b.json", TODAY)
    assert reading["state"] == "new"
    assert json.loads(reading["detail"]) == {
        "after": 5,
        "issues": [
            {
                "number": 7,
                "title": "finding 7",
                "severity": "high",
                "paths": ["scripts/dev/fanout.py"],
                "no_vetted_remediation": True,
                "solo_only": True,
            }
        ],
    }


def _claimed():
    comment = {
        "body": claim_comment(WT, None, "2026-11-14T11:00:00Z"),
        "user": {"login": OPERATOR},
        "author_association": "OWNER",
        "created_at": "2026-11-14T11:00:00Z",
    }
    return (
        _rest_issue(7, comments=1),
        {f"{REG}/issues/7/comments": [comment]},
        live_worktree(WT),
    )


def _blocked():
    blocker = {
        "number": 3,
        "state": "open",
        "html_url": "https://github.com/DanielH2018/server/issues/3",
    }
    return (
        _rest_issue(7, blocked_by=1),
        {f"{REG}/issues/7/dependencies/blocked_by": [blocker]},
        None,
    )


@pytest.mark.parametrize(
    "case",
    [
        _claimed,
        _blocked,
        lambda: (_rest_issue(7), {f"{REG}/pulls": [{"body": "Closes #7"}]}, None),
        lambda: (_rest_issue(7, pr=True), {}, None),
    ],
    ids=["live-claim", "open-blocker", "open-pr-closes-it", "a-pull-request"],
)
def test_a_new_issue_next_would_not_offer_keeps_the_wait_open(tmp_path, case):
    issue, api, worktrees = case()
    tools, _ = _tools([issue, _rest_issue(5)], worktrees=worktrees, **api)
    reading = new_issue_probe.read(tools, 5, tmp_path / "b.json", TODAY)
    assert reading["state"] == "none"
    assert reading["detail"].startswith("no new free issue above #5")


def test_a_blocker_rest_returns_without_a_state_is_refused_not_read_as_unblocked(
    tmp_path,
):
    issue, api, _ = _blocked()
    blocker = api[f"{REG}/issues/7/dependencies/blocked_by"][0]
    del blocker["state"]
    tools, _ = _tools([issue, _rest_issue(5)], **api)
    with pytest.raises(KeyError, match="state"):
        new_issue_probe.read(tools, 5, tmp_path / "b.json", TODAY)


def test_a_failed_github_read_exits_1_with_no_state_line_and_no_baseline(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    error = subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502")
    tools, _ = build_tools(Fakes(json_errors={f"api {REG}/issues": error}))
    assert main(["new-issues"], tools) == 1
    assert capsys.readouterr().out == ""
    assert not list(tmp_path.rglob("*.json"))


def test_with_repo_reads_that_register_and_judges_claims_against_its_checkout(capsys):
    dotfiles = "repos/DanielH2018/dotfiles"
    tools, calls = build_tools(
        Fakes(
            api={f"{dotfiles}/issues": [_rest_issue(7)], f"{dotfiles}/pulls": []},
            worktree_facts=facts(),
        )
    )
    argv = ["new-issues", "--repo", "DanielH2018/dotfiles", "--after", "5"]
    assert main(argv, tools) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "new"
    assert calls.checkouts == [REGISTER_CHECKOUTS["DanielH2018/dotfiles"]]


def test_a_milestone_wait_ends_only_on_a_new_issue_that_milestone_holds(tmp_path):
    backups = {"title": "Backups", "number": 9, "state": "open"}
    issues = [{**_rest_issue(8), "milestone": backups}, _rest_issue(7), _rest_issue(5)]
    tools, _ = _tools(issues, **{f"{REG}/milestones": [[backups]]})
    reading = new_issue_probe.read(tools, 5, tmp_path / "b.json", TODAY, "Backups")
    assert [i["number"] for i in json.loads(reading["detail"])["issues"]] == [8]


def test_a_wait_on_an_unknown_milestone_ends_unknown_milestone(capsys):
    tools, _ = _tools([_rest_issue(7)], **{f"{REG}/milestones": [[]]})
    assert main(["new-issues", "--after", "5", "--milestone", "Backup"], tools) == 0
    assert json.loads(capsys.readouterr().out) == {
        "state": "unknown-milestone",
        "detail": "no milestone is titled `Backup`",
    }


def test_a_milestone_wait_records_a_baseline_of_its_own(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    backups = {"title": "Backups", "number": 9, "state": "open"}
    tools, _ = _tools([_rest_issue(7)], **{f"{REG}/milestones": [[backups]]})
    assert main(["new-issues", "--milestone", "Backups"], tools) == 0
    here = (None, Path.cwd())
    assert new_issue_probe.baseline_file(*here, "Backups").exists()
    assert not new_issue_probe.baseline_file(*here, None).exists()
