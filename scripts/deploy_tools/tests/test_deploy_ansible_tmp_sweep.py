"""`deploy.sh` removes the ansible temp directories a dead run left (issue #5000).

A run killed mid-task leaves an `ansible-tmp-*` holding its module, and any exit that skips
atexit leaves its `ansible-local-*`. The sweep must take those and leave alone a directory
that a live process or a running task may still use.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_ansible_tmp_sweep.py
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from deploy_tools.deploy_lib import ansible_tmp
from lib.repo_paths import REPO

from _deploy_sh_fakes import (
    FAKE_RECAP,
    UV_WRAPPER_ARMS,
    deploy_sh_env,
    make_snapshot_repo,
    stub_bin,
)

DAY = 24 * 60 * 60
# A fixed epoch (2026-09-21), so no distance here spans the suite's own runtime. The wiring
# test's deploy.sh reads the real clock, against which these mtimes are older still.
NOW = 1_790_000_000.0


def _dead_pid() -> int:
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


def _local(parent: Path, pid: int) -> Path:
    """An `ansible-local-*` named the way ansible-core's `mkdtemp` names it."""
    return Path(tempfile.mkdtemp(prefix=f"ansible-local-{pid}", dir=parent))


def _module(parent: Path, pid: int) -> Path:
    path = parent / f"ansible-tmp-{NOW - 2 * DAY}-{pid}-123456789"
    path.mkdir()
    (path / "AnsiballZ_command.py").write_text("")
    return path


def _age(path: Path, seconds: float) -> Path:
    os.utime(path, (NOW - seconds, NOW - seconds))
    return path


@pytest.mark.parametrize(
    ("make", "stale"),
    [
        # A dead run's empty local directory, over a day old: the backlog #4893 left.
        (lambda d: _age(_local(d, _dead_pid()), 2 * DAY), True),
        # A killed task's module directory, over a day old.
        (lambda d: _age(_module(d, os.getpid()), 2 * DAY), True),
        # The same two under a day old: a run may still be using them.
        (lambda d: _age(_local(d, _dead_pid()), DAY / 2), False),
        (lambda d: _age(_module(d, os.getpid()), DAY / 2), False),
        # A live process still owns its directory however old it is.
        (lambda d: _age(_local(d, os.getpid()), 2 * DAY), False),
        # A local directory holding files is not provably abandoned.
        (lambda d: _age(_with_file(_local(d, _dead_pid())), 2 * DAY), False),
        # Anything else in the directory is not ansible's to take.
        (lambda d: _age(_mkdir(d / "ansible-galaxy-cache"), 2 * DAY), False),
    ],
    ids=[
        "old-empty-dead-local",
        "old-module",
        "fresh-local",
        "fresh-module",
        "live-local",
        "non-empty-local",
        "unrelated",
    ],
)
def test_is_stale(tmp_path, make, stale):
    assert ansible_tmp.is_stale(make(tmp_path), NOW) is stale


def _with_file(path: Path) -> Path:
    (path / "leftover").write_text("")
    return path


def _mkdir(path: Path) -> Path:
    path.mkdir()
    return path


def test_the_sweep_removes_the_stale_ones_and_logs_each(tmp_path):
    logged: list[list[str]] = []
    stale = [
        _age(_local(tmp_path, _dead_pid()), 2 * DAY),
        _age(_module(tmp_path, os.getpid()), 2 * DAY),
    ]
    kept = [
        _age(_local(tmp_path, _dead_pid()), DAY / 2),
        _age(_local(tmp_path, os.getpid()), 2 * DAY),
    ]

    assert ansible_tmp.sweep_stale([tmp_path], NOW, logged.append) == 2

    assert not any(p.exists() for p in stale)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(p.name for p in kept)
    # One log call, one line per removed directory with its mtime, none for a kept one.
    [lines] = logged
    assert sorted(lines) == sorted(
        f"removed {p} (mtime 2026-09-19T14:13:20Z)" for p in stale
    )


# The `uv` a `--check` run execs: the playbook answers at once. The wrapper's own
# `deploy_locks.py` call still runs for real, through UV_WRAPPER_ARMS.
_UV_STUB = """#!/bin/bash
case "$*" in
  *ansible-playbook*) {recap}; exit 0 ;;
{locks}
  *) exit 0 ;;
esac
""".replace("{recap}", FAKE_RECAP).replace("{locks}", UV_WRAPPER_ARMS)


def test_a_deploy_sh_run_sweeps_the_ansible_temp_directory(tmp_path, _own_ansible_tmp):
    """The wiring: a real `deploy.sh --check` reaches the sweep before its exec.

    `_own_ansible_tmp` points both of ansible's temp settings at a directory of this test's
    own, so the run sweeps that and never the shared `~/.ansible/tmp`.
    """
    leftover = _age(_module(_own_ansible_tmp, os.getpid()), 2 * DAY)
    live = _age(_local(_own_ansible_tmp, os.getpid()), 2 * DAY)
    env = deploy_sh_env(tmp_path, stub_bin(tmp_path, {"uv": _UV_STUB}))
    result = subprocess.run(
        [
            str(REPO / "scripts" / "deploy.sh"),
            "--skip-tag-check",
            "--skip-staleness-check",
            "--check",
            "--tags",
            "alpha",
        ],
        cwd=make_snapshot_repo(tmp_path / "repo"),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert not leftover.exists()
    assert live.is_dir()
    assert "removed 1 stale ansible temp directories" in result.stderr


def test_the_variable_beats_a_constant_loaded_before_it(monkeypatch, _own_ansible_tmp):
    """A process that imported ansible earlier holds the old parent in its constants.

    A pytest worker is that process: its in-process deploy.sh runs swept the real
    `~/.ansible/tmp` until the variable took precedence.
    """
    constants = SimpleNamespace(DEFAULT_LOCAL_TMP="/elsewhere/ansible-local-1abcdefgh")
    monkeypatch.setitem(sys.modules, "ansible.constants", constants)

    assert ansible_tmp.tmp_dirs() == [_own_ansible_tmp]
