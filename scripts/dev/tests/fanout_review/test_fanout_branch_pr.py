"""A batch's PR is the one GitHub holds for its branch, not the first URL in its final text.

A body that links another PR before its own must not send the review, ready flip and `done`
line there. The pipeline half runs `Pipeline.run_all` through `_review_fakes.FakeRunner`;
the status half runs `status.with_own_pr` over a parsed block.

Run: uv run pytest scripts/dev/tests/fanout_review/test_fanout_branch_pr.py
"""

import dataclasses
import json
import subprocess

from _review_fakes import BRANCH, _finding, _pipeline, _report
from dev.fanout_lib.branch_pr import (
    BranchPrs,
    OpenPr,
    own_pr,
    parse_branch_prs,
    parse_merged_before,
    parse_open_pr,
)
from dev.fanout_lib.manifest import Batch
from dev.fanout_lib.status import parse_status, with_own_pr
from dev.fanout_lib.transport import open_pr_files

FOREIGN = "https://github.com/DanielH2018/server/pull/6449"
OWN = "https://github.com/DanielH2018/server/pull/6523"
# The shape of batch 6309's final text: the PR it measured against, then its own.
TEXT = f"measured against [#6449]({FOREIGN}) ... opened {OWN}"
OWN_ROW = {"url": OWN, "number": 6523}


def _record(tmp_path):
    records = [p for p in (tmp_path / "state").iterdir() if p.suffix == ".json"]
    assert len(records) == 1
    return json.loads(records[0].read_text())


def test_the_first_url_in_the_text_is_not_taken_for_the_branchs_pr(tmp_path):
    reports = [
        _report(TEXT),
        _report(structured={"summary": "", "findings": [_finding("nit", "low")]}),
    ]
    pipeline, run = _pipeline(tmp_path, reports, host="daniel-server")
    run.branch_prs = [OWN_ROW]
    pipeline.run_all()
    assert _record(tmp_path)["pr"] == OWN
    comment = next(a for a in run.gh_calls if a[:3] == ["gh", "pr", "comment"])
    assert comment[3] == OWN
    # The lookup asks about the worktree's own branch, in any PR state.
    assert run.listed[0][run.listed[0].index("--head") + 1] == BRANCH
    assert run.listed[0][run.listed[0].index("--state") + 1] == "all"


def test_a_text_naming_only_another_pr_ends_needs_input_naming_both(tmp_path):
    pipeline, run = _pipeline(tmp_path, [_report(f"Measured against {FOREIGN}.")])
    run.branch_prs = []
    final = pipeline.run_all()
    assert final["result"].startswith("needs input:")
    assert FOREIGN in final["result"] and BRANCH in final["result"]
    assert [phase for _, _, phase in run.claude] == ["implement"]
    assert run.comments == []
    assert _record(tmp_path)["outcome"] == "needs-input"


def test_a_text_naming_no_pr_still_ends_the_run_as_no_pr(tmp_path):
    pipeline, run = _pipeline(tmp_path, [_report("Tests pass, no PR yet.")])
    run.branch_prs = []
    pipeline.run_all()
    assert _record(tmp_path)["outcome"] == "no-pr"
    assert run.listed == []


def test_own_pr_prefers_the_branch_pr_the_text_names_and_reads_a_failed_lookup_as_unknown():
    both = [OWN, FOREIGN]
    assert own_pr(f"{OWN} then {FOREIGN}", "b", both).url == OWN
    assert own_pr(TEXT, "b", [OWN]).url == OWN
    unknown = own_pr(TEXT, "b", None)
    assert unknown.problem and unknown.unanswered
    assert own_pr("nothing", "b", []).problem == ""


def test_parse_branch_prs_lists_open_before_merged_and_drops_closed_ones():
    """An abandoned batch's closed PR keeps its branch name; a relaunch must not bind it."""
    rows = [
        {"url": "closed9", "number": 9, "state": "CLOSED"},
        {"url": "open5", "number": 5, "state": "OPEN"},
        {"url": "merged7", "number": 7, "state": "MERGED"},
    ]
    held = parse_branch_prs(json.dumps(rows))
    assert held == BranchPrs(open=("open5",), merged=("merged7",))
    assert held.urls == ["open5", "merged7"]
    assert parse_branch_prs(json.dumps(rows[:1])) == BranchPrs()


def test_a_relaunch_binds_its_open_pr_not_the_abandoned_closed_one(tmp_path):
    abandoned = "https://github.com/DanielH2018/server/pull/5919"
    reports = [
        _report(f"Replaces the abandoned {abandoned}; opened {OWN}"),
        _report(structured={"summary": "", "findings": []}),
    ]
    pipeline, run = _pipeline(tmp_path, reports, host="daniel-server")
    # The text names the closed PR first and it has the higher number, so only state decides.
    run.branch_prs = [
        {"url": abandoned, "number": 9999, "state": "CLOSED"},
        {"url": OWN, "number": 6438, "state": "OPEN"},
    ]
    pipeline.run_all()
    assert _record(tmp_path)["pr"] == OWN


def test_a_relaunch_whose_only_branch_pr_is_closed_ends_needs_input_not_on_its_ci(
    tmp_path,
):
    """The agent pushed to another branch, so the batch's branch holds only the closed PR."""
    closed = "https://github.com/DanielH2018/server/pull/5919"
    pipeline, run = _pipeline(
        tmp_path, [_report(f"Opened {OWN}")], host="daniel-server"
    )
    run.branch_prs = [{"url": closed, "number": 5919, "state": "CLOSED"}]
    final = pipeline.run_all()
    assert final["result"].startswith("needs input:")
    assert OWN in final["result"] and "no open or merged PR" in final["result"]
    assert run.ci_calls == [] and _record(tmp_path)["outcome"] == "needs-input"


def test_parse_branch_prs_lists_the_newest_first_and_rejects_other_shapes():
    rows = [{"url": "u1", "number": 1}, {"url": "u9", "number": 9}]
    assert parse_branch_prs(json.dumps(rows)) == BranchPrs(open=("u9", "u1"))
    assert parse_branch_prs("not json") is None
    assert parse_branch_prs('{"url": "u"}') is None


B = Batch(
    "b",
    "daniel-server",
    "/home/ubuntu/server/.claude/worktrees/fanout-b",
    "worktree-fanout-b",
    "fanout-b",
    [1],
    "t",
)


def _status(text: str):
    block = (
        "=== b\nActiveState=inactive\nResult=success\nExecMainStatus=0\n"
        "--- stderr\n--- report\n"
        + json.dumps({"type": "result", "result": text})
        + "\n"
    )
    return parse_status([B], block)[0]


def test_status_reports_the_branchs_pr_when_the_text_names_another_first():
    parsed = _status(TEXT)
    assert parsed.pr_url == FOREIGN
    done = with_own_pr(parsed, B.branch, lambda *_: BranchPrs(open=(OWN,)), "o/r")
    assert (done.state, done.pr_url) == ("done", OWN)


def test_status_reads_a_foreign_only_text_as_needs_input_never_done():
    parsed = _status(f"Measured against {FOREIGN}.")
    held = with_own_pr(parsed, B.branch, lambda *_: BranchPrs(), "o/r")
    assert (held.state, held.pr_url) == ("needs-input", "")
    assert (
        FOREIGN in held.final_text
        and "no open or merged PR on GitHub" in held.final_text
    )


def test_status_reads_a_citation_of_a_pr_merged_before_the_launch_as_no_pr():
    """A relaunched batch that opened nothing cites its branch's earlier merged PR."""
    parsed = _status(f"Already fixed by {FOREIGN}.")
    cited = with_own_pr(
        parsed,
        B.branch,
        lambda *_: BranchPrs(merged=(FOREIGN,)),
        "o/r",
        lambda: [FOREIGN],
    )
    assert (cited.state, cited.pr_url) == ("no-pr", "")
    # A merge after the launch is this batch's own landing, which stays its PR.
    own = with_own_pr(
        parsed, B.branch, lambda *_: BranchPrs(merged=(FOREIGN,)), "o/r", lambda: []
    )
    assert (own.state, own.pr_url) == ("done", FOREIGN)


def test_parse_merged_before_keeps_only_merges_older_than_the_launch():
    rows = [
        {"url": "old", "mergedAt": "2026-11-05T18:56:29Z"},
        {"url": "new", "mergedAt": "2026-11-06T11:00:00Z"},
        {"url": "open", "mergedAt": None},
    ]
    launch = "2026-11-06T10:05:15.123456+00:00"
    assert parse_merged_before(json.dumps(rows), launch) == ["old"]
    # An unreadable launch time or list sets nothing aside.
    assert parse_merged_before(json.dumps(rows), "t") == []
    assert parse_merged_before("not json", launch) == []


def test_status_does_not_ask_the_forge_for_a_batch_without_a_pr_url():
    parsed = dataclasses.replace(_status("Tests pass."), pr_url="")

    def refuse(*_):
        raise AssertionError("asked the forge")

    assert with_own_pr(parsed, B.branch, refuse, "o/r") is parsed


def _gh_listing_one_file_of_150(rest):
    """A `gh` whose `pr list` shows 1 of the 150 files BRANCH's PR changes, then `rest`."""
    listed = [
        {
            "headRefName": BRANCH,
            "number": 7,
            "changedFiles": 150,
            "files": [{"path": "docs/a.md"}],
        }
    ]
    calls = []

    def gh(*args, **_):
        calls.append(args[0])
        if args[0] == "pr":
            return subprocess.CompletedProcess(args, 0, json.dumps(listed), "")
        return rest

    return gh, calls


def test_a_pr_past_the_listed_files_is_read_whole_only_for_a_named_branch():
    rest = subprocess.CompletedProcess([], 0, "docs/a.md\nscripts/b.py\n", "")
    gh, calls = _gh_listing_one_file_of_150(rest)
    assert open_pr_files("o/r", run=gh) == {BRANCH: frozenset({"docs/a.md"})}
    assert calls == ["pr"]
    assert open_pr_files("o/r", {BRANCH}, run=gh) == {
        BRANCH: frozenset({"docs/a.md", "scripts/b.py"})
    }


def test_a_pr_whose_remaining_files_cannot_be_read_makes_the_open_prs_unreadable():
    gh, _ = _gh_listing_one_file_of_150(subprocess.CompletedProcess([], 1, "", "502"))
    assert open_pr_files("o/r", {BRANCH}, run=gh) is None


def test_an_open_prs_failed_checks_are_listed_and_a_cancelled_or_passing_one_is_not():
    rollup = [
        {"name": "pytest", "conclusion": "FAILURE", "detailsUrl": "https://ci/1"},
        {"name": "prek", "conclusion": "CANCELLED", "detailsUrl": "https://ci/2"},
        {"name": "lint", "conclusion": "SUCCESS", "detailsUrl": "https://ci/3"},
        {"context": "netlify", "state": "ERROR", "targetUrl": "https://n/4"},
    ]
    stdout = json.dumps([{"url": OWN, "isDraft": True, "statusCheckRollup": rollup}])
    assert parse_open_pr(stdout) == OpenPr(
        OWN, draft=True, failing=("pytest https://ci/1", "netlify https://n/4")
    )


def test_a_branch_with_no_open_pr_reads_none_and_garbage_reads_a_reason():
    assert parse_open_pr("[]") is None
    assert isinstance(parse_open_pr("not json"), str)
