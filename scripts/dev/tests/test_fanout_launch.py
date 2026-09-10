"""Brief, worktree and launch commands, and the manifest — spec §3-4.

Run: uv run pytest scripts/dev/tests/test_fanout_launch.py
"""

import json
from datetime import UTC, datetime

import pytest

from fanout_lib.brief import Issue, render_brief
from fanout_lib.launch import (
    LaunchError,
    create_worktree_command,
    launch,
    launch_command,
    unit_name,
    worktree_path,
)
from fanout_lib.manifest import Batch, Manifest, load, new_run_id, save
from _fanout_fakes import fake_tools, ok

ISSUES = [
    Issue(1345, "Traefik startupProbe has no red-proof", "body one\nline two"),
    Issue(1386, "Healthchecks key", "second body"),
]


def test_daniel_box_brief_lands_and_daniel_server_brief_stops_at_the_pr():
    box = render_brief(ISSUES, "daniel-box", "1345-1386", "worktree-orch", [])
    server = render_brief(ISSUES, "daniel-server", "1345-1386", "worktree-orch", [])
    assert "land.sh" in box and "grep -m1 '^VERDICT:'" in box
    assert "land.sh" not in server and "gh pr create" in server
    assert "do not merge" in server.lower()


def test_both_briefs_carry_issue_bodies_verbatim_and_the_claim_note():
    for host in ("daniel-box", "daniel-server"):
        text = render_brief(
            ISSUES,
            host,
            "1345-1386",
            "worktree-orch",
            ["  ✗ primary checkout is dirty"],
        )
        assert "body one\nline two" in text and "second body" in text
        assert "already claimed under `worktree-orch`" in text
        assert (
            'gh issue comment 1345 --body "Worked by `worktree-fanout-1345-1386`"'
            in text
        )
        assert "findings.py open" in text
        assert "primary checkout is dirty" in text


def test_worktree_and_unit_names_derive_from_the_batch():
    assert (
        worktree_path("1345-1386")
        == "/home/ubuntu/server/.claude/worktrees/fanout-1345-1386"
    )
    assert unit_name("1345-1386") == "fanout-1345-1386"


def test_the_worktree_command_fetches_before_adding_from_origin_master():
    cmd = create_worktree_command("b")
    assert cmd.index("git -C /home/ubuntu/server fetch origin") < cmd.index(
        "worktree add"
    )
    assert "-b worktree-fanout-b" in cmd and cmd.rstrip().endswith("origin/master")


def test_the_launch_command_is_a_transient_user_service_reading_the_brief():
    cmd = launch_command("b")
    assert cmd.startswith("systemd-run --user --unit fanout-b ")
    assert "--scope" not in cmd  # a scope would tie the agent to the launching ssh
    assert "-p WorkingDirectory=/home/ubuntu/server/.claude/worktrees/fanout-b" in cmd
    assert (
        "StandardInput=file:/home/ubuntu/server/.claude/worktrees/fanout-b/.fanout/brief.md"
        in cmd
    )
    assert (
        "StandardOutput=file:/home/ubuntu/server/.claude/worktrees/fanout-b/.fanout/report.json"
        in cmd
    )
    assert "claude -p --model opus --permission-mode auto --output-format json" in cmd


def test_launch_writes_the_brief_over_stdin_then_starts_the_unit():
    tools, run = fake_tools({"daniel-server": ok("")})
    batch = launch(tools, "daniel-server", "b", "BRIEF", issues=[1])
    hosts_cmds = [(h, c) for h, c, _ in run.calls]
    assert [h for h, _ in hosts_cmds] == ["daniel-server"] * 3
    assert "worktree add" in hosts_cmds[0][1]
    assert run.calls[1][2] == "BRIEF" and "cat > " in hosts_cmds[1][1]
    assert hosts_cmds[2][1].startswith("systemd-run")
    assert batch.unit == "fanout-b" and batch.host == "daniel-server"


def test_a_failed_worktree_add_removes_the_half_made_tree_and_launches_nothing():
    import subprocess

    tools, run = fake_tools(
        {
            "daniel-server": subprocess.CompletedProcess(
                [], 128, stdout="", stderr="fatal: branch exists"
            )
        }
    )
    with pytest.raises(LaunchError, match="branch exists"):
        launch(tools, "daniel-server", "b", "BRIEF", issues=[1])
    assert len(run.calls) == 2 and "worktree remove --force" in run.calls[1][1]
    assert not any(c.startswith("systemd-run") for _, c, _ in run.calls)


def test_manifest_round_trips_outside_the_repo(tmp_path):
    now = datetime(2026, 9, 9, 20, 30, tzinfo=UTC)
    m = Manifest(
        new_run_id(now),
        "worktree-orch",
        [
            Batch(
                "b",
                "daniel-box",
                worktree_path("b"),
                "worktree-fanout-b",
                "fanout-b",
                [1],
                now.isoformat(),
            )
        ],
    )
    path = save(m, root=tmp_path)
    assert path == tmp_path / "20260909T203000Z.json"
    assert load("20260909T203000Z", root=tmp_path) == m
    assert json.loads(path.read_text())["batches"][0]["host"] == "daniel-box"
