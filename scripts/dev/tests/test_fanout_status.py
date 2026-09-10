"""status parses one ssh call per host into per-batch verdicts — spec §4-5.

Run: uv run pytest scripts/dev/tests/test_fanout_status.py
"""

import subprocess

from fanout_lib.brief import Issue
from fanout_lib.manifest import Batch, Manifest, save
from fanout_lib.status import parse_status, status_command, stop_command
from fanout_place import main
from _fanout_fakes import fake_tools, ok

B = Batch(
    "b",
    "daniel-box",
    "/home/ubuntu/server/.claude/worktrees/fanout-b",
    "worktree-fanout-b",
    "fanout-b",
    [1],
    "t",
)

RUNNING = "=== b\nActiveState=active\nResult=success\nExecMainStatus=0\n--- stderr\n--- report\n"
DONE = (
    "=== b\nActiveState=inactive\nResult=success\nExecMainStatus=0\n--- stderr\n--- report\n"
    '{"type":"result","result":"Opened https://github.com/DanielH2018/server/pull/1500 for #1."}\n'
)
FAILED = "=== b\nActiveState=failed\nResult=exit-code\nExecMainStatus=1\n--- stderr\nError: not logged in\n--- report\n"
VANISHED = "=== b\nActiveState=inactive\nResult=success\nExecMainStatus=\n--- stderr\n--- report\n"


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


def test_a_vanished_unit_with_no_report_is_failed_not_done():
    assert parse_status([B], VANISHED)[0].state == "failed"


def test_stop_never_removes_the_worktree():
    assert stop_command("b") == "systemctl --user stop fanout-b"


def test_cli_launch_places_and_starts_the_agent_with_one_systemd_run_call(tmp_path):
    tools, run = fake_tools(
        answers={
            "daniel-box": ok("1\n12884901888\n0\n"),
            "daniel-server": ok("1\n12884901888\n0\n"),
        },
        issues=[Issue(1, "t", "b")],
    )
    code = main(
        [
            "launch",
            "--batch",
            "1",
            "--orchestrator-branch",
            "o",
            "--manifest-root",
            str(tmp_path),
        ],
        tools,
    )
    assert code == 0
    systemd_calls = [c for c in run.calls if c[1].startswith("systemd-run")]
    assert len(systemd_calls) == 1
    assert [p.name for p in tmp_path.glob("*.json")]


def test_cli_launch_reports_no_headroom_when_both_hosts_are_uncapped(tmp_path):
    tools, run = fake_tools(
        answers={
            "daniel-box": ok("1\nmax\n0\n"),
            "daniel-server": ok("1\nmax\n0\n"),
        },
        issues=[Issue(1, "t", "b")],
    )
    code = main(
        [
            "launch",
            "--batch",
            "1",
            "--orchestrator-branch",
            "o",
            "--manifest-root",
            str(tmp_path),
        ],
        tools,
    )
    assert code == 3
    assert not [c for c in run.calls if c[1].startswith("systemd-run")]


def test_a_failed_and_a_successful_health_read_both_surface_in_the_brief(tmp_path):
    tools, run = fake_tools(
        answers={"daniel-server": ok("1\n12884901888\n0\n")},
        issues=[Issue(1, "t", "b")],
    )
    run.answers_by_call = [
        ok("1\n12884901888\n0\n"),  # headroom read, daniel-box
        ok("1\n12884901888\n0\n"),  # headroom read, daniel-server
        subprocess.CompletedProcess(
            ["x"], 1, stdout="", stderr="not logged in"
        ),  # daniel-box health
        ok("line one\nline two\n"),  # health read, daniel-server
    ]
    code = main(
        [
            "launch",
            "--batch",
            "1",
            "--orchestrator-branch",
            "o",
            "--manifest-root",
            str(tmp_path),
        ],
        tools,
    )
    assert code == 0
    brief_calls = [c for c in run.calls if c[1].startswith("mkdir -p")]
    assert len(brief_calls) == 1
    brief_stdin = brief_calls[0][2]
    assert (
        "[daniel-box] session-health read failed (exit 1) — banner state unknown"
        in brief_stdin
    )
    assert "[daniel-server] line one" in brief_stdin
    assert "[daniel-server] line two" in brief_stdin


def test_cli_status_reports_a_read_timeout_as_exit_1_not_a_traceback(tmp_path, capsys):
    batch = Batch("1", "daniel-box", "/w/1", "worktree-fanout-1", "fanout-1", [1], "t")
    run = Manifest("20260101T000000Z", "o", [batch])
    save(run, root=tmp_path)
    tools, fake_run = fake_tools()
    fake_run.answers_by_call = [subprocess.TimeoutExpired(cmd="ssh", timeout=30.0)]
    code = main(["status", run.run_id, "--manifest-root", str(tmp_path)], tools)
    assert code == 1
    assert "1 on daniel-box: status read timed out" in capsys.readouterr().out
