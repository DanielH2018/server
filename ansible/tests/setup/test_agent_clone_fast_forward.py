#!/usr/bin/env python3
"""claude-clone-sync.sh fast-forwards the agent's clone only when it is a clean master.

The hook shim and the homelab-ui launcher run from that clone's main working tree, which the
shared agent_user.yml clones once and never pulls (#4067, #4099). claude-clone-sync.timer runs
the script, because a merge that touches only the launcher or `.claude/hooks/` applies no role
(#4162). The script runs here
against real scratch repositories, so a test passes only if the gate decides correctly.

Run: uv run pytest ansible/tests/setup/test_agent_clone_fast_forward.py
"""

import subprocess
from pathlib import Path

import pytest
from _helpers import ANSIBLE
from _setup_render import render_setup_text
from lib.git_testing import commit, git, git_out, init_repo, scrubbed_env
from lib.proc_testing import run, write_exec

ROLE = ANSIBLE / "roles" / "setup" / "claude_code"
SCRIPT = ROLE / "files" / "claude-clone-sync.sh"
LIVE_SCRIPT = "/usr/local/bin/claude-clone-sync.sh"


class Repos:
    """An origin, the agent's clone of it, and a second clone that pushes to origin."""

    def __init__(self, root: Path) -> None:
        self.origin = init_repo(root / "origin.git", bare=True)
        seed = init_repo(root / "seed")
        commit(seed, "seed", **{"README": "1\n", "uv.lock": "a\n"})
        git(seed, "remote", "add", "origin", str(self.origin))
        git(seed, "push", "-q", "origin", "master")
        self.pusher = seed
        self.clone = root / "clone"
        git(root, "clone", "-q", str(self.origin), str(self.clone))
        self.home = root / "home"
        self.calls = root / "calls.log"
        # Stand-ins for the agent's uv and the clone venv's ansible-galaxy, recording their
        # arguments. Untracked, so the status gate never sees them.
        for tool in (
            self.home / ".local/bin/uv",
            self.clone / ".venv/bin/ansible-galaxy",
        ):
            write_exec(tool, f'echo "{tool.name} $*" >> {self.calls}\n')

    def push(self, **files: str) -> str:
        sha = commit(self.pusher, "upstream", **files)
        git(self.pusher, "push", "-q", "origin", "master")
        return sha

    def head(self) -> str:
        return git_out(self.clone, "rev-parse", "HEAD")

    def sync(self) -> subprocess.CompletedProcess[str]:
        return run(
            ["bash", str(SCRIPT), str(self.clone)],
            env=scrubbed_env(HOME=str(self.home)),
        )

    def tool_calls(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []


@pytest.fixture
def repos(tmp_path: Path) -> Repos:
    return Repos(tmp_path)


def test_a_clean_master_advances_to_origin(repos: Repos) -> None:
    before = repos.head()
    target = repos.push(**{"scripts/diagnostics/ui_mcp.sh": "new\n"})
    result = repos.sync()
    assert result.returncode == 0, result.stderr
    assert repos.head() == target
    assert result.stdout == f"advanced {before[:12]}..{target[:12]}\n"
    assert repos.tool_calls() == [], "no lock file moved, so nothing re-syncs"


def test_an_untracked_file_does_not_hold_the_clone_back(repos: Repos) -> None:
    (repos.clone / "scratch.txt").write_text("x\n")
    target = repos.push(README="2\n")
    assert repos.sync().returncode == 0
    assert repos.head() == target


def test_an_up_to_date_clone_reports_so(repos: Repos) -> None:
    result = repos.sync()
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("up to date at ")


def _modified(repos: Repos) -> None:
    (repos.clone / "README").write_text("local edit\n")


def _detached(repos: Repos) -> None:
    git(repos.clone, "checkout", "-q", "--detach")


def _other_branch(repos: Repos) -> None:
    git(repos.clone, "checkout", "-q", "-b", "worktree-foo")


def _named_after_master(repos: Repos) -> None:
    git(repos.clone, "checkout", "-q", "-b", "master-old")


@pytest.mark.parametrize(
    "state",
    [_modified, _detached, _other_branch, _named_after_master],
    ids=["modified file", "detached HEAD", "another branch", "master-old"],
)
def test_the_agents_work_in_progress_is_left_alone(repos: Repos, state) -> None:
    state(repos)
    before = repos.head()
    repos.push(README="2\n")
    result = repos.sync()
    assert result.returncode == 0, "a skip must not fail the unit and page"
    assert result.stdout.startswith("skipped: ")
    assert repos.head() == before


def test_a_second_run_skips_while_the_first_holds_the_lock(repos: Repos) -> None:
    before = repos.head()
    repos.push(README="2\n")
    lock = repos.clone / ".git" / "claude-clone-sync.lock"
    held = run(
        ["flock", str(lock), "bash", str(SCRIPT), str(repos.clone)],
        env=scrubbed_env(HOME=str(repos.home)),
    )
    assert held.stdout.startswith("skipped: another claude-clone-sync run")
    assert repos.head() == before


def test_a_diverged_master_fails(repos: Repos) -> None:
    commit(repos.clone, "local", README="local\n")
    repos.push(README="2\n")
    result = repos.sync()
    assert result.returncode != 0, "a master that cannot fast-forward must page"


def test_a_moved_lock_resyncs_the_venv_and_collections(repos: Repos) -> None:
    repos.push(**{"uv.lock": "b\n", "ansible/requirements.yml": "collections: []\n"})
    assert repos.sync().returncode == 0
    assert repos.tool_calls() == [
        "uv sync --frozen",
        "ansible-galaxy collection install -r ansible/requirements.yml -p ansible/collections",
    ]


def test_the_timer_runs_the_script_as_the_agent() -> None:
    """The timer runs the script as the clone's owner, which git requires."""
    unit = render_setup_text("claude_code", "claude-clone-sync.service.j2", {})
    assert "User=claude\n" in unit
    assert f"ExecStart={LIVE_SCRIPT} /var/lib/claude/server\n" in unit
