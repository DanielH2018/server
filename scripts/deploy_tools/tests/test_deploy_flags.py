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

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_flags.py
"""

import importlib.util
import sys

import pytest

from deploy_tools.deploy_lib import flags as deploy_flags
from lib import exit_codes as ec

from _deploy_sh_fakes import make_snapshot_repo, run_front_half

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
