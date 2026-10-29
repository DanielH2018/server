"""A batch's PR is the one GitHub holds for its branch, not the first URL in its final text (#6551).

Batch 6309's body linked #6449 before its own #6523, so its review, ready flip and `done` line
all went to #6449. The pipeline half runs `Pipeline.run_all` through `_review_fakes.FakeRunner`;
the status half runs `status.with_own_pr` over a parsed block.

Run: uv run pytest scripts/dev/tests/test_fanout_branch_pr.py
"""

import dataclasses
import json

from _review_fakes import BRANCH, _finding, _pipeline, _report
from fanout_lib.branch_pr import own_pr, parse_branch_prs
from fanout_lib.manifest import Batch
from fanout_lib.status import parse_status, with_own_pr

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
    assert "could not be asked" in own_pr(TEXT, "b", None).problem
    assert own_pr("nothing", "b", []).problem == ""


def test_parse_branch_prs_lists_the_newest_first_and_rejects_other_shapes():
    rows = [{"url": "u1", "number": 1}, {"url": "u9", "number": 9}]
    assert parse_branch_prs(json.dumps(rows)) == ["u9", "u1"]
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
    done = with_own_pr(parsed, B.branch, lambda *_: [OWN], "o/r")
    assert (done.state, done.pr_url) == ("done", OWN)


def test_status_reads_a_foreign_only_text_as_needs_input_never_done():
    parsed = _status(f"Measured against {FOREIGN}.")
    held = with_own_pr(parsed, B.branch, lambda *_: [], "o/r")
    assert (held.state, held.pr_url) == ("needs-input", "")
    assert FOREIGN in held.final_text and "no PR on GitHub" in held.final_text
    unread = with_own_pr(parsed, B.branch, lambda *_: None, "o/r")
    assert unread.state == "needs-input"


def test_status_does_not_ask_the_forge_for_a_batch_without_a_pr_url():
    parsed = dataclasses.replace(_status("Tests pass."), pr_url="")

    def refuse(*_):
        raise AssertionError("asked the forge")

    assert with_own_pr(parsed, B.branch, refuse, "o/r") is parsed
