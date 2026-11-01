"""`owed_run.py`: apply exactly what a `manual_plane` line owes, then clear it.

Auto mode allows this command where it allowed no hand-typed apply, so every refusal is a pair
with the clean run: a line it should not trust, or that needs the operator, runs nothing.
Commands go to an injected runner, so no test reaches ansible or a host.

Run: uv run pytest scripts/deploy_tools/tests/test_owed_run.py
"""

import json
import subprocess
from pathlib import Path

import pytest

from deploy_tools import gitops_state, owed_run


def _owed(subject: str, playbook: str, *tags: str, **keys) -> str:
    """One `manual_plane` ledger line owing `tags`, with any `host`/`agent_hosts` keys."""
    line = {"class": "manual_plane", "subject": subject, "origin": "abc123def4567890"}
    return json.dumps(
        {**line, "at": 1000, "playbook": playbook, "tags": sorted(tags), **keys}
    )


COREDNS = _owed("k3s", "ansible/k3s-bringup.yml", "coredns")
HYPERVISOR = _owed("hypervisor", "ansible/initial_setup.yml", host="daniel-server")


class Runner:
    """Records each command and answers with the next exit code, 0 once they run out."""

    def __init__(self, *codes: int):
        self.codes, self.ran = list(codes), []

    def __call__(self, argv, cwd):
        self.ran.append(argv[-1])
        return subprocess.CompletedProcess(argv, self.codes.pop(0) if self.codes else 0)


@pytest.fixture
def owe(tmp_path: Path, tree_lock, journal):
    """`owe(*lines)` -> a `run(role, runner, **kw)` against a ledger holding `lines`."""

    def setup(*lines: str):
        (tmp_path / "owed.jsonl").write_text("".join(f"{line}\n" for line in lines))
        state = gitops_state.DeployerState(str(tmp_path))

        def run(role, runner, checkout_check=lambda repo: None, **kw):
            return owed_run.run_owed(
                state,
                role,
                run=runner,
                checkout_check=checkout_check,
                now=2000,
                lock_path=str(tree_lock),
                lock_wait_s=0.2,
                journal=lambda role, dropped, remaining: journal.append(
                    (role, dropped, remaining)
                ),
                **kw,
            )

        return state, run

    return setup


def test_a_clean_line_runs_its_command_then_clears_it(owe, journal):
    state, run = owe(COREDNS)
    runner = Runner()
    assert run("k3s", runner) == 0
    assert runner.ran == [
        "uv run ansible-playbook ansible/k3s-bringup.yml --tags coredns"
    ]
    assert state.manual_plane_pending() == []
    assert journal[0][0] == "k3s"


def test_a_line_for_another_host_runs_there_after_a_pull(owe):
    _, run = owe(HYPERVISOR)
    runner = Runner()
    assert run("hypervisor", runner) == 0
    assert runner.ran == [
        'ssh daniel-server "cd /home/ubuntu/server && git pull --ff-only && '
        'uv run ansible-playbook ansible/initial_setup.yml --tags hypervisor"'
    ]


def test_a_failed_command_leaves_the_line_pending(owe, journal, capsys):
    state, run = owe(COREDNS)
    assert run("k3s", Runner(2)) == 1
    assert [e.role for e in state.manual_plane_pending()] == ["k3s"]
    assert journal == []
    assert "stays pending" in capsys.readouterr().err


def test_a_dry_run_prints_the_commands_and_changes_nothing(owe, capsys):
    state, run = owe(COREDNS)
    runner = Runner()
    assert run("k3s", runner, dry_run=True) == 0
    assert runner.ran == []
    assert "--tags coredns" in capsys.readouterr().out
    assert [e.role for e in state.manual_plane_pending()] == ["k3s"]


@pytest.mark.parametrize(
    "line, role, why",
    [
        # A host value that would close the ssh quote and run its own command.
        (
            _owed(
                "hypervisor", "ansible/initial_setup.yml", host='daniel-server";id;"'
            ),
            "hypervisor",
            "no role, tag or host spells",
        ),
        (
            _owed("hypervisor", "ansible/initial_setup.yml", host="daniel-laptop"),
            "hypervisor",
            "inventory does not",
        ),
        (
            _owed("tooling", "ansible/../../tmp/x.yml"),
            "tooling",
            "not one the routing records",
        ),
        (_owed("common", "none"), "common", "no recorded command applies it"),
        (_owed("k3s", "ansible/k3s-bringup.yml"), "k3s", "arms more than the change"),
    ],
    ids=[
        "shell-in-host",
        "unknown-host",
        "foreign-playbook",
        "by-hand",
        "maximal-apply",
    ],
)
def test_a_line_it_cannot_trust_or_that_needs_the_operator_runs_nothing(
    owe, capsys, line, role, why
):
    state, run = owe(line)
    runner = Runner()
    assert run(role, runner) == 1
    assert runner.ran == []
    assert why in capsys.readouterr().err
    assert len(state.manual_plane_pending()) == 1


def test_a_checkout_behind_origin_master_runs_nothing(owe, capsys):
    state, run = owe(COREDNS)
    runner = Runner()
    assert (
        run(
            "k3s",
            runner,
            checkout_check=lambda repo: "HEAD abc is not origin/master def",
        )
        == 1
    )
    assert runner.ran == []
    assert "is not origin/master" in capsys.readouterr().err
    assert [e.role for e in state.manual_plane_pending()] == ["k3s"]


@pytest.fixture
def clone(tmp_path: Path):
    """(a checkout at origin/master of a bare `origin`, a `git(*args)` that runs in it)."""
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "PATH": "/usr/bin:/bin",
    }
    work = tmp_path / "work"

    def git(*args, cwd=work):
        subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            check=True,
            capture_output=True,
            timeout=30,
        )

    git("init", "-q", "--bare", "-b", "master", "origin.git", cwd=tmp_path)
    git("clone", "-q", "origin.git", "work", cwd=tmp_path)
    git("commit", "-q", "--allow-empty", "-m", "one")
    git("push", "-q", "origin", "HEAD:master")
    return work, git


def test_a_checkout_at_origin_master_passes_the_check(clone):
    work, _ = clone
    assert owed_run._checkout_is_origin_master(str(work)) is None


def test_a_checkout_ahead_of_origin_master_fails_the_check(clone):
    work, git = clone
    git("commit", "-q", "--allow-empty", "-m", "two")
    why = owed_run._checkout_is_origin_master(str(work))
    assert why is not None and "is not origin/master" in why


def test_a_role_that_is_not_pending_runs_nothing(owe, capsys):
    _, run = owe(COREDNS)
    runner = Runner()
    assert run("tooling", runner) == 1
    assert runner.ran == []
    assert "nothing to run" in capsys.readouterr().err
