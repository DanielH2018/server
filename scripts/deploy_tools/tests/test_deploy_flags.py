#!/usr/bin/env python3
"""`deploy.sh` refuses an argument ansible-playbook's parser rejects, with 64.

ansible-playbook exits 2 on an argparse error, which is the same number it uses for "a host
failed". The wrapper read that 2 as a run that happened: `./scripts/deploy.sh --no-such-flag`
reported `DEPLOY_PLAYBOOK_FAILED (20)` and told the operator that "changes that applied before
the failing task ARE live" and that a re-run is not safe. No play had run -- argparse refused
before the first one -- and `land.sh` exits 64 on a bad argument of its own, so the two entry
points disagreed on the same mistake.

Both halves, per CLAUDE.md: a refused argument must exit 64 having deployed nothing (the half
the bug got wrong), and the pass-through arguments a REAL deploy carries must still reach the
playbook (the half a gate that simply refused everything would pass).

The gate imports ansible-core in the wrapper's own process, and that import creates a temp
directory the `--check` exec must not orphan (issue #4893).

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_flags.py
"""

import importlib.util
import subprocess
import sys

import pytest

from deploy_tools.deploy_lib import flags as deploy_flags
from lib import exit_codes as ec
from lib.repo_paths import REPO

from _deploy_sh_fakes import (
    FAKE_RECAP,
    UV_WRAPPER_ARMS,
    deploy_sh_env,
    make_snapshot_repo,
    run_front_half,
    stub_bin,
)

# The code the wrapper must not return for a usage error: it means changes are live. It
# must never be this gate's answer.
_PLAYBOOK_FAILED = 20


def test_ansibles_parser_is_importable_here():
    """The gate asks `ansible.cli.playbook.PlaybookCLI` and fails OPEN when it cannot import it.

    Asserted by name so nothing below can pass vacuously: with ansible-core off the path the
    gate lets every argument through, and the failures would not say why.
    """
    assert importlib.util.find_spec("ansible.cli.playbook") is not None


@pytest.mark.parametrize("bad", [["--no-such-flag"], ["--tags"], ["-t"]])
def test_an_argument_the_parser_rejects_is_a_usage_error(bad):
    """RED half. `--tags` and `-t` with no value are the shapes an operator types by accident."""
    assert deploy_flags.usage_error(bad) is True


@pytest.mark.parametrize(
    "good",
    [
        [],
        ["--tags", "sonarr", "-e", "target=daniel-pi"],
        ["--check", "--diff", "-vv"],
        # The case that would make this gate worse than the bug it fixes: a vars file that
        # exists only in the snapshot the locked half renders. The parse must not open it.
        ["-e", "@vars/only-in-the-snapshot.yml"],
        ["-i", "inventory/does-not-exist.yml", "--limit", "daniel-box"],
    ],
)
def test_the_arguments_a_real_deploy_carries_parse_clean(good):
    """CLEAN half: only a usage error is one, and a missing file is not a usage error."""
    assert deploy_flags.usage_error(good) is False


def test_the_gate_fails_open_when_ansible_cannot_be_imported(monkeypatch):
    """The DECIDED note in the module: an ansible whose CLI moved leaves the deploy running.

    Refusing every deploy over a renamed ansible module would be worse than the bug.
    """
    monkeypatch.setitem(sys.modules, "ansible.cli.playbook", None)
    assert deploy_flags.usage_error(["--no-such-flag"]) is False


def test_a_refused_argument_exits_64_having_reached_no_gate(tmp_path, monkeypatch):
    """The wrapper's half: 64, and nothing before it -- no staleness gate, no snapshot, no lock.

    `run_front_half` replaces each helper with a recorder, so an empty call list IS the claim
    that a usage error costs no subprocess and no lock wait.
    """
    repo = make_snapshot_repo(tmp_path / "repo")
    code, calls = run_front_half(monkeypatch, repo, ["--no-such-flag"])
    assert code == ec.DEPLOY_BAD_FLAGS, calls
    assert code != _PLAYBOOK_FAILED
    assert [c[0] for c in calls] == [], calls


def test_a_valid_run_still_reaches_the_locked_half(tmp_path, monkeypatch):
    """CLEAN half of the same claim: the gate sits in the path, and lets a real deploy past."""
    repo = make_snapshot_repo(tmp_path / "repo")
    code, calls = run_front_half(
        monkeypatch,
        repo,
        ["--tags", "uptime-kuma", "--skip-tag-check", "-e", "target=daniel-pi", "-vv"],
    )
    assert code is None, calls
    assert ("deploy", ["in-process", "uptime-kuma"]) in calls, calls


# The `uv` a `--check` run execs: the playbook answers at once. The wrapper's own
# `deploy_locks.py` call still runs for real, through UV_WRAPPER_ARMS.
_UV_STUB = """#!/bin/bash
case "$*" in
  *ansible-playbook*) {recap}; exit 0 ;;
{locks}
  *) exit 0 ;;
esac
""".replace("{recap}", FAKE_RECAP).replace("{locks}", UV_WRAPPER_ARMS)


def test_a_check_run_leaves_no_ansible_local_temp_directory(tmp_path):
    """Importing ansible for the gate makes a temp directory that the exec must not orphan.

    ansible-core removes its `ansible-local-<pid>*` directory in an atexit handler, and a
    `--check` run ends in `os.execvp`, which runs none (issue #4893). The variable puts the
    directory under `tmp_path` rather than `~/.ansible/tmp`, which other processes share.
    """
    ansible_tmp = tmp_path / "ansible-tmp"
    env = deploy_sh_env(
        tmp_path,
        stub_bin(tmp_path, {"uv": _UV_STUB}),
        ANSIBLE_LOCAL_TEMP=str(ansible_tmp),
    )
    repo = make_snapshot_repo(tmp_path / "repo")
    deploy_sh = REPO / "scripts" / "deploy.sh"
    result = subprocess.run(
        [
            str(deploy_sh),
            "--skip-tag-check",
            "--skip-staleness-check",
            "--check",
            "--tags",
            "alpha",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    # ansible-core creates the parent when it resolves the setting, so the import happened
    # and the empty listing below cannot pass vacuously.
    assert ansible_tmp.is_dir()
    assert sorted(p.name for p in ansible_tmp.iterdir()) == []
