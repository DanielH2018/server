"""status parses one ssh call per host into per-batch verdicts — spec §4-5.

Run: uv run pytest scripts/dev/tests/test_fanout_status.py
"""

from fanout_lib.brief import Issue
from fanout_lib.manifest import Batch
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
