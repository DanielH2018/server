"""The default views of `place status`, `place runs` and `place launch`: a count, then exceptions.

Run: uv run pytest scripts/dev/tests/fanout_status/test_fanout_status_view.py
"""

import json
import re

from dev.findings_lib.issue_model import Issue, IssueState
from dev.fanout_lib.manifest import SWEEP_KEPT_LIMIT, Batch, Manifest, save
from dev.fanout_lib.place import main
from dev.fanout_lib.status_view import runs_lines, status_lines
from _fanout_fakes import HOST_KEY, fake_tools, ok

PR = "https://github.com/o/r/pull/9"
HEADROOM = f"1\n12884901888\n1\n12884901888\n0\n{HOST_KEY}\n"


def _row(batch, state, pr_url="", text="", unconfirmed=False):
    line = f"{batch} on daniel-box: {state}"
    if pr_url:
        line += f" {pr_url}"
    if text:
        line += f" {text}"
    return {
        "batch": batch,
        "state": state,
        "pr_url": pr_url,
        "line": line,
        "pr_unconfirmed": unconfirmed,
    }


ROWS = [
    _row("1", "landed", PR),
    _row("2", "cleaned"),
    _row("3", "running"),
    _row("4", "done", PR, "Opened it. Then filed #2."),
    _row("5", "needs-input", "", "which host?"),
]


def test_status_counts_a_settled_batch_and_lists_the_rest():
    assert status_lines(ROWS, show_all=False) == [
        "1 landed, 1 cleaned, 1 running, 1 done, 1 needs-input",
        "3 on daniel-box: running",
        f"4 on daniel-box: done {PR}",
        "5 on daniel-box: needs-input which host?",
    ]


def test_status_all_lists_every_batch_in_full_after_the_count():
    lines = status_lines(ROWS, show_all=True)
    assert lines[1:] == [r["line"] for r in ROWS]


def test_a_done_batch_keeps_its_permission_denials_and_drops_the_text():
    row = _row("4", "done", PR, "permission_denials=2 Opened it.")
    assert status_lines([row], show_all=False)[1] == (
        f"4 on daniel-box: done {PR} permission_denials=2"
    )


def test_a_fully_settled_run_prints_the_count_line_alone():
    assert status_lines(ROWS[:2], show_all=False) == ["1 landed, 1 cleaned"]


def test_status_json_still_returns_every_row(tmp_path, capsys):
    cleaned = Batch("1", "daniel-box", "/w1", "b1", "u1", [1], "2026", "2026-10-09")
    save(Manifest("20260101T000010Z", "o", [cleaned]), root=tmp_path)
    tools, _ = fake_tools()
    argv = ["status", "20260101T000010Z", "--json", "--manifest-root", str(tmp_path)]
    assert main(argv, tools) == 0
    assert [r["state"] for r in json.loads(capsys.readouterr().out)] == ["cleaned"]


# A batch on the landing host that opened its PR and whose land log holds a verdict.
DONE = (
    "=== 1\nActiveState=inactive\nResult=success\nExecMainStatus=0\n--- stderr\n--- report\n"
    + json.dumps({"type": "result", "result": f"Opened {PR}."})
    + "\n--- verdict\nVERDICT: settled\n"
)


def test_status_keeps_a_done_batch_and_its_url_when_github_cannot_be_asked(
    tmp_path, capsys
):
    done = Batch("1", "daniel-box", "/w1", "b1", "u1", [1], "t")
    save(Manifest("20260101T000011Z", "o", [done]), root=tmp_path)
    tools, _ = fake_tools(answers={"daniel-box": ok(DONE)}, branch_prs={"b1": None})
    argv = ["status", "20260101T000011Z", "--json", "--manifest-root", str(tmp_path)]
    assert main(argv, tools) == 0
    [row] = json.loads(capsys.readouterr().out)
    assert (row["state"], row["pr_url"], row["pr_unconfirmed"]) == ("done", PR, True)


def test_status_names_the_batches_github_could_not_be_asked_about_once():
    rows = [
        _row("3", "running"),
        _row("4", "done", PR, unconfirmed=True),
        _row("6", "done", PR, unconfirmed=True),
    ]
    count, *batches = status_lines(rows, show_all=False)
    counts, _, note = count.partition("; ")
    assert counts == "1 running, 2 done"
    assert "4, 6" in note
    assert batches == [
        "3 on daniel-box: running",
        f"4 on daniel-box: done {PR}",
        f"6 on daniel-box: done {PR}",
    ]


def _runs():
    standing = Batch("1", "daniel-box", "/w1", "worktree-fanout-1", "u1", [1], "t")
    cleaned = Batch("2", "daniel-box", "/w2", "b2", "u2", [2], "t", "2026-10-09")
    return [
        Manifest("20260101T000010Z", "worktree-a", [standing, cleaned]),
        Manifest("20260102T000010Z", "worktree-b", [cleaned]),
    ]


def test_runs_lists_a_standing_batch_and_counts_the_cleaned_ones():
    assert runs_lines(_runs(), show_all=False) == [
        "1 standing, 2 cleaned",
        "20260101T000010Z  orchestrator worktree-a",
        "  1 on daniel-box: worktree-fanout-1 #1 standing",
        "1 run(s) with no standing batch; --all lists them",
    ]


def test_runs_all_lists_every_run_and_batch():
    lines = runs_lines(_runs(), show_all=True)
    assert "20260102T000010Z  orchestrator worktree-b" in lines
    assert "  2 on daniel-box: b2 #2 cleaned 2026-10-09" in lines


def test_runs_names_a_batch_the_sweep_stopped_on_with_its_kept_line():
    stopped = Batch("3", "daniel-server", "/w3", "worktree-fanout-3", "u3", [3], "t")
    for _ in range(SWEEP_KEPT_LIMIT):
        stopped = stopped.kept_by_clean("kept: /w3 not merged")
    lines = runs_lines([Manifest("20260103T000010Z", "c", [stopped])], show_all=False)
    assert lines == [
        "1 sweep-stopped",
        "20260103T000010Z  orchestrator c",
        "  3 on daniel-server: worktree-fanout-3 #3 sweep-stopped: kept: /w3 not merged",
    ]


def test_launch_prints_one_line_with_the_run_id_and_each_batchs_host_and_rung(
    tmp_path, capsys
):
    issues = [
        Issue(1, "one", IssueState.OPEN, body="body one", labels=frozenset({"claude"}))
    ]
    tools, run = fake_tools(answers={"daniel-box": ok(HEADROOM)}, issues=issues)
    run.answers_by_call = [ok(HEADROOM), ok(""), ok("")]
    argv = ["launch", "--batch", "1", "--host", "daniel-box", "--manifest-root"]
    assert main([*argv, str(tmp_path)], tools) == 0
    out = capsys.readouterr().out
    assert re.fullmatch(
        r"run \d{8}T\d{6}Z: 1 -> daniel-box \(opus-medium\); claims held under `worktree-orch`\n",
        out,
    )


def test_status_prints_the_count_alone_for_a_run_of_cleaned_batches(tmp_path, capsys):
    cleaned = Batch("1", "daniel-box", "/w1", "b1", "u1", [1], "2026", "2026-10-09")
    save(Manifest("20260101T000010Z", "o", [cleaned]), root=tmp_path)
    tools, _ = fake_tools()
    argv = ["status", "20260101T000010Z", "--manifest-root", str(tmp_path)]
    assert main(argv, tools) == 0
    assert capsys.readouterr().out == "1 cleaned\n"
