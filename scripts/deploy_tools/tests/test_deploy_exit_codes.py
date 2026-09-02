#!/usr/bin/env python3
"""Tests for scripts/deploy.sh's exit-code contract.

The failure this guards is an exit code that means the OPPOSITE of what a consumer reads it
as. ansible-playbook returns 2 on a failed host, 3 on an unreachable one and 4 on a parse
error; deploy.sh reserves 2/3/4 for a tag miss, a broad change and a stale tree, all three of
which mean nothing was deployed. Until 2026-09-02 the wrapper returned ansible's status
verbatim, so a play that applied its manifests and then failed on a post-apply assert exited 2
and `land.sh` reported "a derived tag matched no service, so nothing deployed" (issue #840).

Every rule here has both halves, per CLAUDE.md: a playbook failure must NOT read as a wrapper
refusal, and a real wrapper refusal must still read as itself. Without the second half a table
that simply stopped matching would look fixed.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_exit_codes.py
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_DEPLOY_SH = _REPO / "scripts" / "deploy.sh"

# deploy.sh's own contract. PLAYBOOK_FAILED must stay outside the wrapper's refusal codes --
# that disjointness IS the fix, so it is asserted rather than assumed.
_PLAYBOOK_FAILED = 20
_WRAPPER_REFUSALS = (2, 3, 4, 75)

_FLOCK_STUB = """#!/bin/bash
# Drop flock's own flags and its lock-file argument, then run the rest.
while [[ $# -gt 0 ]]; do
  case "$1" in
    -w|-E) shift 2 ;;
    -n|-u) shift ;;
    *) break ;;
  esac
done
shift
exec "$@"
"""

_UV_STUB = """#!/bin/bash
# Only the playbook run carries the exit code under test; the wrapper's own helper calls
# (fact_cache_guard, deploy_tags) must succeed or the script never reaches it.
case "$*" in
  *ansible-playbook*) exit {ansible_exit} ;;
  *) exit 0 ;;
esac
"""


def _run_with_stubs(tmp_path: Path, ansible_exit: int) -> subprocess.CompletedProcess:
    """Run deploy.sh for real, with `uv` and `flock` stubbed on PATH.

    The stubs are the smallest possible: `flock` drops its own options and execs the command it
    was given (so no real /var/lock/server-git-tree.lock is taken and no live deploy can
    interleave), and `uv` exits `ansible_exit` for the playbook run while succeeding for the
    wrapper's helper calls. Everything between -- the argument parsing, the annotation, the exit
    mapping -- is the real script.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "flock").write_text(_FLOCK_STUB)
    (bin_dir / "uv").write_text(_UV_STUB.format(ansible_exit=ansible_exit))
    for stub in ("flock", "uv"):
        (bin_dir / stub).chmod(0o755)

    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
    return subprocess.run(
        [
            str(_DEPLOY_SH),
            "--tags",
            "uptime-kuma",
            "--skip-tag-check",
            "--skip-staleness-check",
        ],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_playbook_failed_is_outside_every_wrapper_refusal_code():
    """The disjointness the fix rests on. A collision here is the bug returning."""
    assert _PLAYBOOK_FAILED not in _WRAPPER_REFUSALS
    assert _PLAYBOOK_FAILED not in (0, 1, 64)


@pytest.mark.parametrize("ansible_exit", [2, 3, 4])
def test_a_failed_playbook_is_flagged_as_a_playbook_failure(tmp_path, ansible_exit):
    """RED half: ansible's 2/3/4 must not be handed out as the wrapper's own 2/3/4.

    All three are parametrized because the collision is not specific to 2 -- ansible's
    unreachable-host (3) and parse-error (4) codes alias the broad-change and stale-tree
    refusals the same way, and both of those tell an operator "nothing was deployed".
    """
    result = _run_with_stubs(tmp_path, ansible_exit)
    assert result.returncode == _PLAYBOOK_FAILED, result.stderr
    assert "the playbook ran and failed" in result.stderr
    assert f"ansible-playbook exit {ansible_exit}" in result.stderr
    # The claim that must never reach an operator on this path.
    assert "nothing was deployed" not in result.stderr.lower()


def test_a_successful_playbook_is_clean(tmp_path):
    """CLEAN half: the mapping must not turn a finished deploy into a failure."""
    result = _run_with_stubs(tmp_path, 0)
    assert result.returncode == 0, result.stderr


def test_a_real_tag_miss_is_still_exit_2():
    """CLEAN half for the code the bug borrowed.

    Run without stubs: the tag validation happens before the lock and before any playbook, so
    this touches nothing. If exit 2 ever stopped meaning a tag miss, the fix above would have
    been a rename rather than a separation.
    """
    result = subprocess.run(
        [str(_DEPLOY_SH), "--tags", "definitely-not-a-real-service"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 2, result.stdout + result.stderr
