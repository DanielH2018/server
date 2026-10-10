"""status parses one ssh call per host into per-batch verdicts — spec §4-5.

Run: uv run pytest scripts/dev/tests/test_fanout_status.py
"""

import dataclasses
import subprocess

from fanout_lib.brief import Issue
from fanout_lib.manifest import Batch, Manifest, save
from fanout_lib.status import (
    parse_status,
    status_command,
    status_line,
    stop_command,
)
from fanout_place import main
from _fanout_fakes import HOST_KEY, fake_tools, ok

B = Batch(
    "b",
    "daniel-box",
    "/home/ubuntu/server/.claude/worktrees/fanout-b",
    "worktree-fanout-b",
    "fanout-b",
    [1],
    "t",
)
# Fleet current, fleet cap, plane current, plane cap, live agents, signing key — the shape
# transport.HOST_READ_COMMAND prints. Roomy on both planes and holding a registered key, so
# these tests measure status parsing rather than placement or the signing gate.
HEADROOM = f"1\n12884901888\n1\n12884901888\n0\n{HOST_KEY}\n"

RUNNING = "=== b\nActiveState=active\nResult=success\nExecMainStatus=0\n--- stderr\n--- report\n"
# `B` runs on the landing host, so a finished batch carries `land.sh`'s verdict as well as
# the PR url — the `--- verdict` section `_one` greps out of the batch's land log.
LANDED = "--- verdict\nVERDICT: settled\n"
DONE = (
    "=== b\nActiveState=inactive\nResult=success\nExecMainStatus=0\n--- stderr\n--- report\n"
    '{"type":"result","result":"Opened https://github.com/DanielH2018/server/pull/1500 for #1."}\n'
    + LANDED
)
# The same batch, stopped the moment `gh pr create` returned: a PR and no verdict anywhere.
OPENED_BUT_NOT_LANDED = (
    "=== b\nActiveState=inactive\nResult=success\nExecMainStatus=0\n--- stderr\n--- report\n"
    '{"type":"result","result":"Opened https://github.com/DanielH2018/server/pull/1500 for #1."}\n'
)
FAILED = "=== b\nActiveState=failed\nResult=exit-code\nExecMainStatus=1\n--- stderr\nError: not logged in\n--- report\n"
VANISHED = "=== b\nActiveState=inactive\nResult=success\nExecMainStatus=\n--- stderr\n--- report\n"
# Batch E of run 20260910T190414Z: the agent removed its own worktree on exit, taking
# .fanout/report.json with it, so the unit exited 0 and there is nothing left to read.
# The blank line between the sections is `_one`'s bare `echo`, so an empty stderr log
# reads as empty rather than swallowing the next marker.
FINISHED_AND_TIDIED = (
    "=== b\nActiveState=inactive\nResult=success\nExecMainStatus=0\n"
    "--- stderr\n\n--- report\n"
)
DONE_WITH_EQUALS_IN_RESULT = (
    "=== b\nActiveState=inactive\nResult=success\nExecMainStatus=0\n--- stderr\n--- report\n"
    '{"type":"result","result":"Fixed the === marker parsing bug; opened https://github.com/o/r/pull/9"}\n'
    + LANDED
)
DONE_WITH_STRAY_MARKER_IN_STDERR = (
    "=== b\nActiveState=inactive\nResult=success\nExecMainStatus=0\n"
    "--- stderr\n=== 12 boom\n--- report\n"
    '{"type":"result","result":"Opened https://github.com/o/r/pull/9"}\n' + LANDED
)
# The same shape as DONE — the unit exited 0 and systemd calls it a success — but the
# session itself reports failure, the way renovate_agent's agent_logic reads that stream.
DONE_SHAPED_BUT_ERRORED = (
    "=== b\nActiveState=inactive\nResult=success\nExecMainStatus=0\n--- stderr\n--- report\n"
    '{"type":"result","result":"gave up part way","is_error":true,'
    '"terminal_reason":"max turns reached",'
    '"permission_denials":[{"tool_name":"Bash"},{"tool_name":"Write"}]}\n'
)
DONE_WITH_MULTILINE_RESULT = (
    "=== b\nActiveState=inactive\nResult=success\nExecMainStatus=0\n--- stderr\n--- report\n"
    '{"type":"result","result":"Opened https://github.com/o/r/pull/9.\\nThen filed #2."}\n'
    + LANDED
)
# A property whose own value carries a section marker. The `=== ` and `--- ` siblings are
# already anchored at start-of-line for this shape; the property split was not.
DONE_WITH_MARKER_INSIDE_A_PROPERTY = (
    "=== b\nActiveState=inactive\nResult=success\n"
    "Description=fanout --- stderr sink\nExecMainStatus=0\n"
    "--- stderr\n--- report\n"
    '{"type":"result","result":"Opened https://github.com/o/r/pull/9"}\n' + LANDED
)

# A turn that ended on a progress report exits the process as cleanly as a
# finished one, and the final text is non-empty either way.
STOPPED_ON_A_PROGRESS_REPORT = (
    "=== b\nActiveState=inactive\nResult=success\nExecMainStatus=0\n--- stderr\n--- report\n"
    '{"type":"result","result":"Tests pass. Next I will open the PR."}\n'
)
STOPPED_ON_A_BLOCKER = (
    "=== b\nActiveState=inactive\nResult=success\nExecMainStatus=0\n--- stderr\n--- report\n"
    '{"type":"result","result":"Fixed the parser.\\nneeds input: which host owns the apply?"}\n'
)
# `RuntimeMaxSec=` expired: systemd stopped the unit, and the claude process it killed wrote
# no report. The exit status can still read 0 when claude exits cleanly on SIGTERM.
TIMED_OUT = (
    "=== b\nActiveState=failed\nResult=timeout\nExecMainStatus=0\n"
    "--- stderr\n\n--- report\n"
)


def _launch(tools, tmp_path, *extra):
    return main(
        [
            "launch",
            "--batch",
            "1",
            "--manifest-root",
            str(tmp_path),
            *extra,
        ],
        tools,
    )


def _brief(run) -> str:
    # The brief is the stdin of the one launch call, identified by the `cat > ` step in
    # its combined worktree-add+lock, brief-write, systemd-run chain.
    briefs = [c[2] for c in run.host_calls if "cat > " in c[1]]
    assert len(briefs) == 1
    return briefs[0]


def test_status_command_is_one_string_naming_every_unit():
    cmd = status_command(
        [B, Batch("c", "daniel-box", "/w/c", "br", "fanout-c", [2], "t")]
    )
    assert "\n" not in cmd and "fanout-b" in cmd and "fanout-c" in cmd
    assert "systemctl --user show fanout-b" in cmd and "tail -c 2000" in cmd


def test_running_done_and_failed_are_told_apart():
    assert parse_status([B], RUNNING)[0].state == "running"
    done = parse_status([B], DONE)[0]
    assert (
        done.state == "done"
        and done.pr_url == "https://github.com/DanielH2018/server/pull/1500"
    )
    failed = parse_status([B], FAILED)[0]
    assert (
        failed.state == "failed"
        and failed.exit_code == 1
        and "not logged in" in failed.stderr_tail
    )


def test_a_unit_that_exited_zero_with_no_report_is_not_failed():
    # The accepting half. Nothing here says the batch failed: the exit code is 0 and the
    # session never set is_error. Only the forge can settle it, so it gets its own state.
    assert parse_status([B], FINISHED_AND_TIDIED)[0].state == "no-report"


def test_a_vanished_unit_with_no_report_is_not_failed_either():
    assert parse_status([B], VANISHED)[0].state == "no-report"


def test_a_batch_with_no_block_in_the_reply_stays_failed():
    # Not the tidied-worktree shape: no block at all means the read itself came back
    # without this batch, which is a defect in the read rather than a finished agent.
    assert parse_status([B], "=== other\nActiveState=inactive\n")[0].state == "failed"


def test_status_line_names_the_branch_it_could_not_reconcile():
    unresolved = parse_status([B], FINISHED_AND_TIDIED)[0]
    line, tier = status_line(unresolved, "daniel-box", B.branch, lambda *_: "", str)
    assert tier == 1
    assert line == f"b on daniel-box: no-report (exit 0)  no merged PR for {B.branch}"


def test_a_nonzero_exit_stays_failed():
    # The rejecting half of the pair above: a state split that swallowed this one would
    # read `no-report` for every crashed batch too.
    assert parse_status([B], FAILED)[0].state == "failed"


def test_a_session_that_reported_is_error_stays_failed():
    assert parse_status([B], DONE_SHAPED_BUT_ERRORED)[0].state == "failed"


def test_a_result_containing_equals_marker_text_is_not_mis_split():
    done = parse_status([B], DONE_WITH_EQUALS_IN_RESULT)[0]
    assert done.state == "done"
    assert done.pr_url == "https://github.com/o/r/pull/9"


def test_a_stray_marker_line_inside_stderr_does_not_split_the_block():
    done = parse_status([B], DONE_WITH_STRAY_MARKER_IN_STDERR)[0]
    assert done.state == "done"
    assert "=== 12 boom" in done.stderr_tail


def test_a_done_shaped_block_whose_result_reports_an_error_is_failed():
    # The pair: the same unit properties read `done` without is_error, so this asserts the
    # is_error arm fires rather than that everything reads failed.
    errored = parse_status([B], DONE_SHAPED_BUT_ERRORED)[0]
    assert errored.state == "failed" and errored.is_error
    assert errored.terminal_reason == "max turns reached"
    assert errored.permission_denials == 2
    clean = parse_status([B], DONE)[0]
    assert clean.state == "done" and not clean.is_error
    assert clean.permission_denials == 0 and clean.terminal_reason == ""


def test_a_property_value_holding_the_section_marker_does_not_truncate_the_properties():
    done = parse_status([B], DONE_WITH_MARKER_INSIDE_A_PROPERTY)[0]
    assert done.state == "done" and done.exit_code == 0


def test_a_final_text_without_a_pr_url_is_not_done():
    """The flagged half; DONE above is the clean half with the same unit properties."""
    stopped = parse_status([B], STOPPED_ON_A_PROGRESS_REPORT)[0]
    assert stopped.state == "no-pr" and stopped.pr_url == ""
    assert parse_status([B], DONE)[0].state == "done"


def test_a_final_text_naming_a_blocker_line_reads_needs_input():
    assert parse_status([B], STOPPED_ON_A_BLOCKER)[0].state == "needs-input"


def test_a_unit_stopped_by_its_runtime_cap_is_failed_not_tidied_up():
    # The pair is FINISHED_AND_TIDIED: the same empty report with Result=success reads
    # `no-report`.
    assert parse_status([B], TIMED_OUT)[0].state == "failed"
    assert parse_status([B], FINISHED_AND_TIDIED)[0].state == "no-report"


def test_a_clean_finish_without_a_pr_prints_its_final_text_and_exits_1():
    for block, state in (
        (STOPPED_ON_A_PROGRESS_REPORT, "no-pr"),
        (STOPPED_ON_A_BLOCKER, "needs-input"),
    ):
        st = parse_status([B], block)[0]
        line, tier = status_line(st, "daniel-box", B.branch, lambda *_: "", str)
        assert tier == 1
        assert line.startswith(f"b on daniel-box: {state} ")
        assert st.final_text in line


def test_stop_never_removes_the_worktree():
    assert stop_command("fanout-b") == "systemctl --user stop fanout-b"


def test_cli_launch_places_and_starts_the_agent_with_one_launch_call(tmp_path):
    tools, run = fake_tools(
        answers={"daniel-box": ok(HEADROOM), "daniel-server": ok(HEADROOM)},
        issues=[Issue(1, "t", "b", ("claude",))],
    )
    assert _launch(tools, tmp_path, "--host", "daniel-box") == 0
    # headroom read, health read, one combined launch call for the one batch.
    assert len(run.host_calls) == 3
    assert [p.name for p in tmp_path.glob("*.json")]


def test_cli_launch_reports_no_headroom_when_the_host_is_uncapped(tmp_path):
    tools, run = fake_tools(
        answers={"daniel-box": ok(f"1\nmax\n1\nmax\n0\n{HOST_KEY}\n")},
        issues=[Issue(1, "t", "b", ("claude",))],
    )
    assert _launch(tools, tmp_path) == 3
    # NoHeadroom stops before any launch call.
    assert not any("worktree add" in c[1] for c in run.host_calls)


def test_a_failed_and_a_successful_health_read_both_surface_in_the_brief(tmp_path):
    # Only the placed host is read, so each half of this is its own run — and its own
    # manifest root, since batch 1 is still live in the first half's manifest and `launch`
    # refuses a batch id live in any run under the root it is given.
    tools, run = fake_tools(
        answers={"daniel-box": ok(HEADROOM)},
        issues=[Issue(1, "t", "b", ("claude",))],
    )
    run.answers_by_call = [
        ok(HEADROOM),
        subprocess.CompletedProcess(["x"], 1, stdout="", stderr="not logged in"),
    ]
    assert _launch(tools, tmp_path, "--host", "daniel-box") == 0
    assert (
        "[daniel-box] session-health read failed (exit 1) — banner state unknown"
        in _brief(run)
    )

    tools, run = fake_tools(
        answers={"daniel-server": ok(HEADROOM)},
        issues=[Issue(1, "t", "roles/k8s/sonarr/tasks/main.yml", ("claude",))],
    )
    run.answers_by_call = [ok(HEADROOM), ok("sonarr line one\nsonarr line two\n")]
    assert _launch(tools, tmp_path / "second-run", "--host", "daniel-server") == 0
    brief = _brief(run)
    assert "[daniel-server] sonarr line one" in brief
    assert "[daniel-server] sonarr line two" in brief


def test_a_health_read_timeout_surfaces_in_the_brief(tmp_path):
    tools, run = fake_tools(
        answers={"daniel-box": ok(HEADROOM)},
        issues=[Issue(1, "t", "b", ("claude",))],
    )
    run.answers_by_call = [
        ok(HEADROOM),
        subprocess.TimeoutExpired(cmd="ssh", timeout=40.0),
    ]
    assert _launch(tools, tmp_path, "--host", "daniel-box") == 0
    assert "session-health read failed (timed out)" in _brief(run)


def test_cli_status_reports_a_read_timeout_as_exit_1_not_a_traceback(tmp_path, capsys):
    batch = Batch("1", "daniel-box", "/w/1", "worktree-fanout-1", "fanout-1", [1], "t")
    run = Manifest("20260101T000000Z", "o", [batch])
    save(run, root=tmp_path)
    tools, fake_run = fake_tools()
    fake_run.answers_by_call = [subprocess.TimeoutExpired(cmd="ssh", timeout=30.0)]
    code = main(["status", run.run_id, "--manifest-root", str(tmp_path)], tools)
    assert code == 1
    assert "1 on daniel-box: status read timed out" in capsys.readouterr().out


def test_cli_status_prints_exit_unknown_for_a_vanished_unit(tmp_path, capsys):
    run = Manifest("20260101T000002Z", "o", [B])
    save(run, root=tmp_path)
    tools, _ = fake_tools(answers={"daniel-box": ok(VANISHED)})
    code = main(["status", run.run_id, "--manifest-root", str(tmp_path)], tools)
    # Tier 1, not 5 — unresolved, not failed.
    assert code == 1
    assert "(exit unknown)" in capsys.readouterr().out


def test_cli_status_reads_a_tidied_up_batch_as_landed_when_its_pr_merged(
    tmp_path, capsys
):
    run = Manifest("20260101T000009Z", "o", [B])
    save(run, root=tmp_path)
    tools, _ = fake_tools(
        answers={"daniel-box": ok(FINISHED_AND_TIDIED)},
        merged_prs={B.branch: "https://github.com/o/r/pull/1699"},
    )
    code = main(["status", run.run_id, "--manifest-root", str(tmp_path)], tools)
    assert code == 0
    assert "b on daniel-box: landed https://github.com/o/r/pull/1699" in (
        capsys.readouterr().out
    )


def test_cli_status_keeps_a_tidied_up_batch_unresolved_when_no_pr_merged(
    tmp_path, capsys
):
    run = Manifest("20260101T000010Z", "o", [B])
    save(run, root=tmp_path)
    tools, _ = fake_tools(answers={"daniel-box": ok(FINISHED_AND_TIDIED)})
    code = main(["status", run.run_id, "--manifest-root", str(tmp_path)], tools)
    assert code == 1
    out = capsys.readouterr().out
    assert "b on daniel-box: no-report" in out
    assert f"no merged PR for {B.branch}" in out


def test_cli_status_still_exits_5_for_a_batch_that_crashed(tmp_path, capsys):
    # A merged PR for the branch does not launder a non-zero exit: the reconciliation is
    # reached only from `no-report`, never from `failed`.
    run = Manifest("20260101T000011Z", "o", [B])
    save(run, root=tmp_path)
    tools, _ = fake_tools(
        answers={"daniel-box": ok(FAILED)},
        merged_prs={B.branch: "https://github.com/o/r/pull/1699"},
    )
    code = main(["status", run.run_id, "--manifest-root", str(tmp_path)], tools)
    assert code == 5
    assert "b on daniel-box: failed (exit 1)" in capsys.readouterr().out


def test_cli_status_prints_the_final_text_on_one_line_when_a_batch_is_done(
    tmp_path, capsys
):
    run = Manifest("20260101T000003Z", "o", [B])
    save(run, root=tmp_path)
    tools, _ = fake_tools(answers={"daniel-box": ok(DONE_WITH_MULTILINE_RESULT)})
    code = main(["status", run.run_id, "--manifest-root", str(tmp_path)], tools)
    assert code == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 1
    assert out[0].endswith("Opened https://github.com/o/r/pull/9. Then filed #2.")


def test_cli_status_exits_5_and_names_the_terminal_reason_on_a_failed_batch(
    tmp_path, capsys
):
    run = Manifest("20260101T000004Z", "o", [B])
    save(run, root=tmp_path)
    tools, _ = fake_tools(answers={"daniel-box": ok(DONE_SHAPED_BUT_ERRORED)})
    code = main(["status", run.run_id, "--manifest-root", str(tmp_path)], tools)
    assert code == 5
    out = capsys.readouterr().out
    assert "b on daniel-box: failed" in out
    assert "permission_denials=2" in out and "max turns reached" in out


def test_cli_status_reports_a_cleaned_batch_without_reading_the_host(tmp_path, capsys):
    """F6: `clean` resets the unit and deletes report.json, which parse_status reads as failed."""
    cleaned = Batch(
        "b",
        "daniel-box",
        "/w",
        "worktree-fanout-b",
        "fanout-b",
        [1],
        "t",
        "2026-09-10T12:00:00+00:00",
    )
    run = Manifest("20260101T000005Z", "o", [cleaned])
    save(run, root=tmp_path)
    tools, calls = fake_tools()
    code = main(["status", run.run_id, "--manifest-root", str(tmp_path)], tools)
    assert code == 0
    assert not calls.calls
    assert (
        "b on daniel-box: cleaned (2026-09-10T12:00:00+00:00)"
        in capsys.readouterr().out
    )


def test_a_landing_host_batch_with_a_pr_and_no_verdict_is_not_done(tmp_path, capsys):
    """The flagged half; DONE is the clean half with the same unit properties."""
    stopped = parse_status([B], OPENED_BUT_NOT_LANDED)[0]
    assert stopped.state == "no-verdict"
    assert stopped.pr_url == "https://github.com/DanielH2018/server/pull/1500"
    line, tier = status_line(stopped, "daniel-box", B.branch, lambda *_: "", str)
    assert tier == 1
    assert stopped.pr_url in line and "land.sh printed no VERDICT" in line
    assert parse_status([B], DONE)[0].state == "done"


def test_the_agents_own_verdict_line_finishes_a_batch_whose_land_log_was_unreadable():
    echoed = OPENED_BUT_NOT_LANDED.replace('for #1."}', 'for #1.\\nVERDICT: settled"}')
    assert parse_status([B], echoed)[0].state == "done"


def test_a_daniel_server_batch_is_done_on_the_pr_url_alone():
    """It is told to stop at `gh pr create`, so there is no verdict for it to owe."""
    remote = Batch(
        "b", "daniel-server", "/w/b", "worktree-fanout-b", "fanout-b", [1], "t"
    )
    assert parse_status([remote], OPENED_BUT_NOT_LANDED)[0].state == "done"


def test_status_reads_the_land_log_over_the_same_call():
    cmd = status_command([B])
    assert "land*.log" in cmd and "'^VERDICT:'" in cmd


def test_a_dotfiles_batch_on_the_landing_host_is_done_on_the_pr_url_alone():
    """The clean half of `test_a_landing_host_batch_with_a_pr_and_no_verdict_is_not_done`."""
    dotfiles = dataclasses.replace(B, repo="DanielH2018/dotfiles")
    assert parse_status([dotfiles], OPENED_BUT_NOT_LANDED)[0].state == "done"


def test_a_tidied_up_batch_is_reconciled_against_its_own_repo():
    unresolved = parse_status([B], FINISHED_AND_TIDIED)[0]
    asked = []
    status_line(
        unresolved,
        "daniel-box",
        B.branch,
        lambda branch, repo: asked.append((branch, repo)) or "",
        str,
        "DanielH2018/dotfiles",
    )
    assert asked == [(B.branch, "DanielH2018/dotfiles")]
