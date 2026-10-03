"""Worktree and launch commands, and the manifest — spec §3-4.

The brief `render_brief` writes is `test_fanout_brief.py`.

Run: uv run pytest scripts/dev/tests/test_fanout_launch.py
"""

import json
import subprocess
from datetime import UTC, datetime

import pytest

from fanout_lib.brief import Issue
from fanout_lib.launch import (
    BUDGET_USD,
    RUNTIME_MAX_S,
    SYSTEM_PROMPT_FILE,
    LaunchError,
    create_worktree_command,
    fast_forward_primary_command,
    launch,
    launch_command,
    remove_worktree_command,
    systemd_run_command,
    unit_name,
    worktree_path,
    write_brief_command,
)
from fanout_lib.manifest import Batch, Manifest, load, new_run_id, save
from _fanout_fakes import fake_tools, ok

from lib.repo_paths import REPO as REPO_ROOT

ISSUES = [
    Issue(1345, "Traefik startupProbe has no red-proof", "body one\nline two"),
    Issue(1386, "Healthchecks key", "second body"),
]


def test_argv_picks_ssh_for_a_remote_host_and_bash_for_the_local_one():
    # Assert on the pure argv builder rather than driving run_command through a real ssh
    # call: leakguard shims `ssh` on PATH and fails the test at teardown the moment it's
    # invoked — stubbed or not — for having "reached outside the test process".
    from fanout_lib.transport import SSH_OPTS, _argv

    assert _argv("daniel-server", "echo hi", "daniel-box") == [
        "ssh",
        *SSH_OPTS,
        "daniel-server",
        "echo hi",
    ]
    assert _argv("daniel-box", "echo hi", "daniel-box") == ["bash", "-c", "echo hi"]


def test_the_local_leg_pins_the_user_bus_when_the_shell_has_none():
    """FLAGGED half: an interactive session's shell exports neither variable, and
    `systemd-run --user` then dies with "Failed to connect to bus: No medium found"."""
    from fanout_lib.transport import local_env

    env = local_env({"PATH": "/usr/bin"}, 1000)
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1000"
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/1000/bus"
    assert env["PATH"] == "/usr/bin"


def test_the_local_leg_keeps_a_bus_the_shell_already_named():
    """CLEAN half: a shell that set its own runtime dir gets a bus derived from THAT, and a
    bus it named outright is left alone."""
    from fanout_lib.transport import local_env

    derived = local_env({"XDG_RUNTIME_DIR": "/run/user/7"}, 1000)
    assert derived["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/7/bus"
    named = local_env({"DBUS_SESSION_BUS_ADDRESS": "unix:path=/x"}, 1000)
    assert named["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/x"
    assert named["XDG_RUNTIME_DIR"] == "/run/user/1000"


def test_run_command_hands_the_pinned_bus_to_the_local_child(monkeypatch):
    """The builder above is what the local leg actually runs under: a child started from a
    shell with both stripped still sees both. `bash -c echo` stays inside the process tree,
    so leakguard has nothing to object to."""
    import os

    from fanout_lib.transport import run_command

    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    proc = run_command(
        "here",
        'echo "$XDG_RUNTIME_DIR $DBUS_SESSION_BUS_ADDRESS"',
        10.0,
        local_host="here",
    )
    uid = os.getuid()
    assert proc.stdout.strip() == f"/run/user/{uid} unix:path=/run/user/{uid}/bus"


def test_worktree_and_unit_names_derive_from_the_batch():
    assert (
        worktree_path("1345-1386")
        == "/home/ubuntu/server/.claude/worktrees/fanout-1345-1386"
    )
    assert unit_name("1345-1386") == "fanout-1345-1386"


def test_the_existence_check_is_the_first_step_of_the_launch_command():
    """Before `fetch`, so a relaunch never reaches the `worktree add` whose cleanup deletes."""
    cmd = launch_command("b", "daniel-server")
    assert cmd.index("fanout-step: exists") < cmd.index(
        "git -C /home/ubuntu/server fetch origin"
    )
    assert cmd.startswith(f"test ! -e {worktree_path('b')} && ")
    assert "! git -C /home/ubuntu/server show-ref --verify --quiet " in cmd
    assert "refs/heads/worktree-fanout-b" in cmd


def test_an_existing_worktree_or_branch_is_refused_without_removing_anything():
    tools, run = fake_tools(
        {
            "daniel-server": subprocess.CompletedProcess(
                [], 1, stdout="", stderr="fanout-step: exists\n"
            )
        }
    )
    with pytest.raises(LaunchError) as excinfo:
        launch(tools, "daniel-server", "b", "BRIEF", issues=[1])
    message = str(excinfo.value)
    assert "worktree-fanout-b" in message and "clean <run-id>" in message
    # The one call is the refused launch itself: no cleanup, so the earlier batch's tree
    # and branch are still there to inspect or land.
    assert len(run.calls) == 1
    assert not any("worktree remove" in c[1] for c in run.calls)


def test_the_worktree_command_fetches_before_adding_from_origin_master():
    cmd = create_worktree_command("b", "daniel-server")
    assert cmd.index("git -C /home/ubuntu/server fetch origin") < cmd.index(
        "worktree add"
    )
    assert "-b worktree-fanout-b" in cmd and "origin/master" in cmd
    # The lock follows the add so a merged-worktree prune never sees the tree unlocked.
    assert cmd.index("worktree add") < cmd.index("worktree lock")
    assert f"worktree lock --reason fanout-b {worktree_path('b')}" in cmd
    # Each step is sentinel-wrapped so a failure can be attributed to it (launch.py's
    # `_step`); the lock step, being last, ends the whole command.
    assert cmd.rstrip().endswith('fanout-step: worktree lock" >&2; exit 1; }')


def test_the_primary_checkout_is_fast_forwarded_between_the_fetch_and_the_add():
    """Hooks are named by an absolute path into the primary checkout.

    A worktree cut from a fresher `origin/master` than the primary checkout registers hook
    scripts that checkout lacks, and every skipped guard is a non-blocking hook error Claude
    Code allows the call past.
    """
    cmd = create_worktree_command("b", "daniel-server")
    assert cmd.index("fanout-step: fetch") < cmd.index("merge --ff-only origin/master")
    assert cmd.index("merge --ff-only origin/master") < cmd.index("worktree add")
    assert "fanout-step: primary ff" in cmd


def test_the_fast_forward_refuses_a_primary_checkout_that_is_not_on_master():
    """`merge --ff-only origin/master` on another branch takes master's commits onto it."""
    cmd = fast_forward_primary_command("daniel-server")
    gate = (
        "git -C /home/ubuntu/server symbolic-ref --quiet --short HEAD | grep -qx master"
    )
    assert cmd.index(gate) < cmd.index("merge --ff-only origin/master")


def test_the_deploy_host_is_not_fast_forwarded_by_a_launch():
    """The CLEAN half. daniel-box's tick pulls every 10 minutes and holds the tree lock to do
    it; a launch cannot hold that lock inside LAUNCH_TIMEOUT_S, so it moves no HEAD there."""
    assert fast_forward_primary_command("daniel-box") == ""
    cmd = create_worktree_command("b", "daniel-box")
    assert "merge --ff-only" not in cmd and "primary ff" not in cmd
    # Still one chain, with no empty element a `&&` would refuse.
    assert " &&  && " not in cmd


def test_a_failed_fast_forward_raises_with_no_cleanup():
    """The tree was never created, so cleanup would fail its own `worktree remove`. The
    refusal is the point: a primary checkout that cannot fast-forward has unknown hook state,
    and a batch placed there runs its guards from whatever is on disk."""
    tools, run = fake_tools(
        {
            "daniel-server": subprocess.CompletedProcess(
                [],
                1,
                stdout="",
                stderr="fatal: Not possible to fast-forward\nfanout-step: primary ff\n",
            )
        }
    )
    with pytest.raises(LaunchError, match="primary ff") as excinfo:
        launch(tools, "daniel-server", "b", "BRIEF", issues=[1])
    assert "Not possible to fast-forward" in str(excinfo.value)
    assert len(run.calls) == 1
    assert not any("worktree remove" in c[1] for c in run.calls)


def test_the_systemd_run_command_is_a_transient_user_service_reading_the_brief():
    cmd = systemd_run_command("b")
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


def test_the_unit_is_bounded_by_a_runtime_cap_and_a_budget():
    """Nothing bounded a headless batch's wall clock or its spend."""
    cmd = systemd_run_command("b")
    assert f"-p RuntimeMaxSec={RUNTIME_MAX_S} " in cmd
    assert cmd.index("RuntimeMaxSec") < cmd.index("claude -p")
    assert f"--max-budget-usd {BUDGET_USD}" in cmd


def test_the_appended_system_prompt_file_exists_where_the_unit_resolves_it():
    """The path is relative to the unit's WorkingDirectory, the worktree root. A rename that
    missed `SYSTEM_PROMPT_FILE` would make every launch fail at unit start."""
    cmd = systemd_run_command("b")
    assert f"--append-system-prompt-file {SYSTEM_PROMPT_FILE}" in cmd
    prompt = REPO_ROOT / SYSTEM_PROMPT_FILE
    assert prompt.is_file()
    assert "needs input:" in prompt.read_text()


def test_the_launch_command_folds_every_step_into_one_call_ending_in_systemd_run():
    cmd = launch_command("b", "daniel-server")
    assert cmd == create_worktree_command(
        "b", "daniel-server"
    ) + " && " + write_brief_command("b") + " && " + systemd_run_command("b")
    assert (
        cmd.index("worktree add")
        < cmd.index("worktree lock")
        < cmd.index("cat > ")
        < cmd.index("systemd-run")
    )


def test_launch_writes_the_brief_over_stdin_then_starts_the_unit():
    tools, run = fake_tools({"daniel-server": ok("")})
    batch = launch(tools, "daniel-server", "b", "BRIEF", issues=[1])
    # One ssh call per batch: add+lock, brief write and systemd-run folded into it.
    assert len(run.calls) == 1
    host, cmd, stdin = run.calls[0]
    assert host == "daniel-server" and stdin == "BRIEF"
    assert cmd.index("worktree add") < cmd.index("cat > ") < cmd.index("systemd-run")
    assert batch.unit == "fanout-b" and batch.host == "daniel-server"


def test_a_failed_worktree_add_removes_the_half_made_tree_and_launches_nothing():
    tools, run = fake_tools(
        {
            "daniel-server": subprocess.CompletedProcess(
                [],
                1,
                stdout="",
                stderr="fatal: branch exists\nfanout-step: worktree add\n",
            )
        }
    )
    with pytest.raises(LaunchError, match="branch exists"):
        launch(tools, "daniel-server", "b", "BRIEF", issues=[1])
    # Two calls total: the failed launch attempt, then cleanup. `&&` between every step of
    # `launch_command` means systemd-run structurally cannot have run — the fake has no way
    # to observe that, since it answers the whole chain with one canned result.
    assert len(run.calls) == 2
    # The cleanup command chains removal and branch deletion with `&&`, not `;` — a bare
    # `;` would force-delete the branch even when the tree was never created.
    assert run.calls[1][1] == remove_worktree_command("b")


def test_a_failed_cleanup_is_folded_into_the_launch_error():
    tools, run = fake_tools()
    run.answers_by_call = [
        subprocess.CompletedProcess(
            [], 1, stdout="", stderr="fatal: branch exists\nfanout-step: worktree add\n"
        ),
        subprocess.CompletedProcess(
            [], 128, stdout="", stderr="fatal: not a working tree"
        ),
    ]
    with pytest.raises(LaunchError) as excinfo:
        launch(tools, "daniel-server", "b", "BRIEF", issues=[1])
    assert "branch exists" in str(excinfo.value)
    assert "not a working tree" in str(excinfo.value)


def test_a_failed_fetch_raises_with_no_cleanup():
    # A fetch failure created nothing — cleanup would only fail its own `worktree remove`
    # with a confusing "not a working tree", so it's skipped.
    tools, run = fake_tools()
    run.answers_by_call = [
        subprocess.CompletedProcess(
            [],
            1,
            stdout="",
            stderr="fatal: unable to access origin\nfanout-step: fetch\n",
        ),
    ]
    with pytest.raises(LaunchError, match="fetch") as excinfo:
        launch(tools, "daniel-server", "b", "BRIEF", issues=[1])
    assert "unable to access origin" in str(excinfo.value)
    assert len(run.calls) == 1


def test_a_failed_worktree_lock_still_cleans_up():
    tools, run = fake_tools({"daniel-server": ok("")})
    run.answers_by_call = [
        subprocess.CompletedProcess(
            [],
            1,
            stdout="",
            stderr="fatal: already locked\nfanout-step: worktree lock\n",
        ),
    ]
    with pytest.raises(LaunchError, match="worktree lock") as excinfo:
        launch(tools, "daniel-server", "b", "BRIEF", issues=[1])
    assert "already locked" in str(excinfo.value)
    assert len(run.calls) == 2
    assert run.calls[1][1] == remove_worktree_command("b")


def test_a_launch_timeout_still_cleans_up_and_raises():
    # A timeout carries no stderr to attribute to a step, but systemd-run starts the unit
    # and returns immediately, so a 120s hang is a stuck `git fetch`/`worktree add` in
    # practice — treated the same as a worktree-add failure, cleanup included.
    tools, run = fake_tools({"daniel-server": ok("")})
    run.answers_by_call = [subprocess.TimeoutExpired(cmd="git", timeout=120.0)]
    with pytest.raises(LaunchError, match="timed out"):
        launch(tools, "daniel-server", "b", "BRIEF", issues=[1])
    assert len(run.calls) == 2 and "worktree remove --force" in run.calls[1][1]


def test_a_failed_brief_write_raises_and_skips_systemd_run():
    # A `cat > path` failure at the redirect never prints "cat:" — bash reports its own
    # "Permission denied" — which is exactly why attribution reads the sentinel, not the
    # tool's own stderr text.
    tools, run = fake_tools()
    run.answers_by_call = [
        subprocess.CompletedProcess(
            [],
            1,
            stdout="",
            stderr=(
                "bash: line 5: /w/.fanout/brief.md: Permission denied\n"
                "fanout-step: brief write\n"
            ),
        ),
    ]
    with pytest.raises(LaunchError, match="brief write") as excinfo:
        launch(tools, "daniel-server", "b", "BRIEF", issues=[1])
    assert "Permission denied" in str(excinfo.value)
    # One call: the worktree add+lock already succeeded (the chain reached `cat`), so it
    # stays for inspection — no cleanup call.
    assert len(run.calls) == 1


def test_a_failed_systemd_run_raises_with_its_stderr():
    tools, run = fake_tools()
    run.answers_by_call = [
        subprocess.CompletedProcess(
            [],
            1,
            stdout="",
            stderr=(
                "Failed to start transient service unit: "
                "Unit fanout-b.service already exists.\n"
                "fanout-step: systemd-run\n"
            ),
        ),
    ]
    with pytest.raises(LaunchError, match="systemd-run") as excinfo:
        launch(tools, "daniel-server", "b", "BRIEF", issues=[1])
    assert "Unit fanout-b.service already exists." in str(excinfo.value)
    # The worktree stays for inspection after a systemd-run failure — no cleanup command.
    assert len(run.calls) == 1


def test_an_unattributable_failure_reports_launch_command_failed_with_no_cleanup():
    # No `fanout-step:` sentinel at all — the ssh connection itself failed before the
    # remote chain ever ran, so there's no step to attribute and nothing to clean up.
    tools, run = fake_tools()
    run.answers_by_call = [
        subprocess.CompletedProcess(
            [],
            255,
            stdout="",
            stderr="ssh: connect to host daniel-server port 22: Connection refused",
        ),
    ]
    with pytest.raises(LaunchError, match="launch command failed") as excinfo:
        launch(tools, "daniel-server", "b", "BRIEF", issues=[1])
    assert "Connection refused" in str(excinfo.value)
    assert len(run.calls) == 1


def test_a_failed_add_and_a_timed_out_cleanup_are_both_reported():
    tools, run = fake_tools()
    run.answers_by_call = [
        subprocess.CompletedProcess(
            [], 1, stdout="", stderr="fatal: branch exists\nfanout-step: worktree add\n"
        ),
        subprocess.TimeoutExpired(cmd="git", timeout=120.0),
    ]
    with pytest.raises(LaunchError) as excinfo:
        launch(tools, "daniel-server", "b", "BRIEF", issues=[1])
    assert "branch exists" in str(excinfo.value)
    assert "timed out" in str(excinfo.value)


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


def _one_batch_manifest(run_id="20260101T000000Z"):
    return Manifest(run_id, "o", [Batch("b", "h", "/w", "br", "u", [1], "t")])


def test_saving_a_manifest_leaves_no_temporary_file_behind(tmp_path):
    save(_one_batch_manifest(), root=tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == ["20260101T000000Z.json"]


def test_a_save_that_fails_mid_write_leaves_the_previous_manifest_intact(tmp_path):
    """`clean` saves after every removal, so a crash mid-write is not a rare edge.

    An in-place write would truncate the file: `remote_fanout_lines` then skips the run in
    the SessionStart banner without a word, and `cmd_clean` cannot load it at all, leaving
    the worktrees it named locked on another host with nothing pointing at them.
    """

    first = _one_batch_manifest()
    manifest_file = save(first, root=tmp_path)
    before = manifest_file.read_text()

    def boom(_src, _dst):
        raise OSError("disk full")

    second = Manifest(first.run_id, "o", [])
    with pytest.raises(OSError):
        save(second, root=tmp_path, replace=boom)
    assert manifest_file.read_text() == before
    assert load(first.run_id, root=tmp_path) == first
    # The failed attempt cleans up after itself rather than leaving a stray .tmp.
    assert [p.name for p in tmp_path.iterdir()] == [manifest_file.name]


def test_a_manifest_written_before_repo_existed_loads_as_this_repo(tmp_path):
    batch = {
        "batch": "b",
        "host": "daniel-box",
        "worktree": "/w",
        "branch": "worktree-fanout-b",
        "unit": "fanout-b",
        "issues": [1],
        "launched_at": "t",
    }
    (tmp_path / "20260101T000000Z.json").write_text(
        json.dumps(
            {
                "run_id": "20260101T000000Z",
                "orchestrator_branch": "o",
                "batches": [batch],
            }
        )
    )
    assert (
        load("20260101T000000Z", root=tmp_path).batches[0].repo == "DanielH2018/server"
    )
