"""A stopped batch pages on every state but `done`, from its unit's ExecStopPost= (#4602).

Run: uv run pytest scripts/dev/tests/test_fanout_notify.py
"""

import json
import shlex
import subprocess
from pathlib import Path

import pytest

from fanout_lib.brief import LANDS
from fanout_lib.launch import snapshot_root, systemd_run_command, worktree_path
from fanout_lib.manifest import Batch
from fanout_lib.notify import NOTIFY_SCRIPT, PAGED_STATES, notify, page_command
from fanout_lib.target import Target
from lib.repo_paths import REPO as REPO_ROOT

B = Batch("b", LANDS, "/wt", "worktree-fanout-b", "fanout-b", [1], "t")
PR = "https://github.com/DanielH2018/server/pull/1500"
EXITED_0 = {"SERVICE_RESULT": "success", "EXIT_CODE": "exited", "EXIT_STATUS": "0"}


def fanout(tmp_path: Path, result: str | None, verdict: str = "") -> Path:
    d = tmp_path / ".fanout"
    d.mkdir()
    if result is not None:
        line = {"type": "result", "result": result}
        (d / "report.json").write_text(json.dumps(line) + "\n")
    if verdict:
        (d / "land1.log").write_text(f"deploying\n{verdict}\n")
    return d


class Recorder:
    def __init__(self, returncode: int = 0):
        self.calls: list[list[str]] = []
        self.returncode = returncode

    def __call__(self, argv, **_):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, self.returncode, "", "Access denied")


def no_merge(branch: str, repo: str) -> str:
    return ""


@pytest.mark.parametrize(
    ("result", "env", "state"),
    [
        (
            "needs input: the operator must pick a webhook route",
            EXITED_0,
            "needs-input",
        ),
        ("failed: CI is red on master", EXITED_0, "needs-input"),
        ("Next I will open the PR.", EXITED_0, "no-pr"),
        (f"Opened {PR}.", EXITED_0, "no-verdict"),
        (
            None,
            {"SERVICE_RESULT": "exit-code", "EXIT_CODE": "exited", "EXIT_STATUS": "1"},
            "failed",
        ),
        # RuntimeMaxSec= expired: systemd killed the session and recorded a timeout.
        (
            None,
            {"SERVICE_RESULT": "timeout", "EXIT_CODE": "killed", "EXIT_STATUS": "TERM"},
            "failed",
        ),
        (None, EXITED_0, "no-report"),
    ],
)
def test_every_state_but_done_pages_once_naming_the_state(
    tmp_path, result, env, state
) -> None:
    run = Recorder()
    line = notify(B, env, fanout(tmp_path, result), no_merge, run)
    assert run.calls == [page_command("fanout-b", state)], line
    assert state in PAGED_STATES
    assert run.calls[0][-1] == f"fanout-alert@fanout-b:{state}.service"
    assert "--no-block" in run.calls[0]


def test_a_landed_batch_pages_nothing(tmp_path) -> None:
    run = Recorder()
    d = fanout(tmp_path, f"Opened {PR}.", verdict="VERDICT: settled")
    assert notify(B, EXITED_0, d, no_merge, run) == "fanout-b: done, no page"
    assert run.calls == []


def test_a_tidied_batch_whose_pr_merged_pages_nothing(tmp_path) -> None:
    # No report, because the agent removed it, and the forge holds the merged PR.
    run = Recorder()
    line = notify(B, EXITED_0, fanout(tmp_path, None), lambda *_: PR, run)
    assert line == "fanout-b: landed, no page"
    assert run.calls == []


@pytest.mark.parametrize(
    "env",
    [
        # `claude` traps the stop job's SIGTERM and exits 143.
        {"SERVICE_RESULT": "exit-code", "EXIT_CODE": "exited", "EXIT_STATUS": "143"},
        # A review unit's Python dies by the signal.
        {"SERVICE_RESULT": "success", "EXIT_CODE": "killed", "EXIT_STATUS": "TERM"},
    ],
)
def test_a_batch_stopped_on_request_pages_nothing(tmp_path, env) -> None:
    run = Recorder()
    assert "stopped on request" in notify(B, env, fanout(tmp_path, None), no_merge, run)
    assert run.calls == []


def test_a_runtime_cap_expiry_still_pages_though_it_sends_the_same_sigterm(tmp_path):
    run = Recorder()
    env = {"SERVICE_RESULT": "timeout", "EXIT_CODE": "exited", "EXIT_STATUS": "143"}
    notify(B, env, fanout(tmp_path, None), no_merge, run)
    assert run.calls == [page_command("fanout-b", "failed")]


def test_a_page_polkit_refuses_is_logged(tmp_path) -> None:
    run = Recorder(returncode=1)
    line = notify(B, EXITED_0, fanout(tmp_path, "needs input: x"), no_merge, run)
    assert line == "fanout-b: needs-input, page FAILED to start: Access denied"


def stop_post_property(cmd: str) -> str:
    argv = shlex.split(cmd.split(" || ", 1)[0])
    (prop,) = [a for a in argv if a.startswith("ExecStopPost=")]
    return prop


@pytest.mark.parametrize("review", [False, True])
def test_every_unit_runs_the_notifier_when_it_stops(review) -> None:
    prop = stop_post_property(systemd_run_command("b", review=review))
    # `-`: a tree the agent already removed fails the chdir, and that must not fail the unit.
    assert prop.startswith('ExecStopPost=-/bin/sh -c "exec uv run --no-project ')
    assert (
        f" {NOTIFY_SCRIPT} --batch b --unit fanout-b --branch worktree-fanout-b "
        "--repo DanielH2018/server " in prop
    )
    # systemd opens the unit's file: outputs without truncating them, so a line on stdout
    # would overwrite the head of report.json.
    assert prop.endswith(' >>.fanout/notify.log 2>&1"')
    assert (REPO_ROOT / NOTIFY_SCRIPT).is_file()


def test_another_repos_unit_runs_the_notifier_from_its_snapshot() -> None:
    dotfiles = Target("DanielH2018/dotfiles", "/c", "origin/main")
    prop = stop_post_property(systemd_run_command("763", dotfiles))
    snapshot = snapshot_root(worktree_path("763", dotfiles))
    assert (
        f" {snapshot}/{NOTIFY_SCRIPT} --batch 763 --unit fanout-dotfiles-763 " in prop
    )
