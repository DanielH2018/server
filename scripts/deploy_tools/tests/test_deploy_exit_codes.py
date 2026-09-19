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

import subprocess
from pathlib import Path

import pytest

from _deploy_sh_fakes import (
    FAKE_RECAP,
    FLOCK_STUB,
    UV_DEPLOY_LOCKS_ARM,
    deploy_sh_env,
    make_snapshot_repo,
)

_REPO = Path(__file__).resolve().parents[3]
_DEPLOY_SH = _REPO / "scripts" / "deploy.sh"

# deploy.sh's own contract. PLAYBOOK_FAILED must stay outside the wrapper's refusal codes --
# that disjointness IS the fix, so it is asserted rather than assumed.
_PLAYBOOK_FAILED = 20
_NO_HOSTS_MATCHED = 78
_WRAPPER_REFUSALS = (2, 3, 4, 75, 76, 77, _NO_HOSTS_MATCHED, 79)

_UV_STUB = """#!/bin/bash
# Only the playbook run carries the exit code under test; the wrapper's own helper calls
# (fact_cache_guard, deploy_tags) must succeed or the script never reaches it.
case "$*" in
  *ansible-playbook*) {recap}; exit {ansible_exit} ;;
{locks}
  *) exit 0 ;;
esac
""".replace("{locks}", UV_DEPLOY_LOCKS_ARM)

# What ansible prints when no play matched a host: the banner, and nothing under it. It exits
# 0 for this, which is the whole reason the wrapper reads the recap (issue #1814).
_EMPTY_RECAP = 'echo "PLAY RECAP *********"'
# A run that never reached the recap at all: killed mid-play, or a parse error before any play.
_NO_RECAP = "true"


def _run_with_stubs(
    tmp_path: Path, ansible_exit: int, recap: str = FAKE_RECAP
) -> subprocess.CompletedProcess:
    """Run deploy.sh for real, with `uv` and `flock` stubbed on PATH.

    The stubs are the smallest possible: `flock` drops its own options and execs the command it
    was given (so no real /var/lock/server-git-tree.lock is taken and no live deploy can
    interleave), and `uv` prints `recap` then exits `ansible_exit` for the playbook run while
    succeeding for the wrapper's helper calls. Everything between -- the argument parsing, the
    annotation, the exit mapping -- is the real script.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "flock").write_text(FLOCK_STUB)
    (bin_dir / "uv").write_text(_UV_STUB.format(ansible_exit=ansible_exit, recap=recap))
    for stub in ("flock", "uv"):
        (bin_dir / stub).chmod(0o755)

    repo = make_snapshot_repo(tmp_path / "repo")
    env = deploy_sh_env(tmp_path, bin_dir)
    return subprocess.run(
        [
            str(_DEPLOY_SH),
            "--tags",
            "uptime-kuma",
            "--skip-tag-check",
            "--skip-staleness-check",
        ],
        cwd=repo,
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


def test_no_hosts_matched_is_outside_every_other_code():
    """78 must alias nothing: not a refusal, not 20, not ansible's own 0-4."""
    assert _NO_HOSTS_MATCHED not in (0, 1, 2, 3, 4, 20, 64, 75, 76, 77)


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


def test_a_recap_naming_no_host_is_not_a_deploy(tmp_path):
    """RED half for issue #1814: ansible exits 0 having matched no host; the wrapper must not.

    This is the run that landed PR #1812 as `settled` with the 2.38.6 pods still up: an
    empty PLAY RECAP, exit 0, and a health gate that read the old workloads as healthy.
    """
    result = _run_with_stubs(tmp_path, 0, recap=_EMPTY_RECAP)
    assert result.returncode == _NO_HOSTS_MATCHED, result.stderr
    assert "matched NO host" in result.stderr
    assert "nothing was deployed" in result.stderr


def test_an_exit_0_with_no_recap_at_all_is_not_a_deploy(tmp_path):
    """ansible always prints a recap when it finishes; exit 0 without one is the same fault."""
    result = _run_with_stubs(tmp_path, 0, recap=_NO_RECAP)
    assert result.returncode == _NO_HOSTS_MATCHED, result.stderr


def test_a_failed_run_with_a_hostless_recap_deployed_nothing(tmp_path):
    """A recap with no host means no task ran, whatever ansible returned -- so 78, not 20,
    whose text promises that changes ARE live."""
    result = _run_with_stubs(tmp_path, 2, recap=_EMPTY_RECAP)
    assert result.returncode == _NO_HOSTS_MATCHED, result.stderr
    assert "ARE live" not in result.stderr


def test_a_failed_run_that_never_reached_the_recap_keeps_the_playbook_failure(tmp_path):
    """CLEAN half of the recap rule: killed mid-play, tasks may have applied -- still 20."""
    result = _run_with_stubs(tmp_path, 2, recap=_NO_RECAP)
    assert result.returncode == _PLAYBOOK_FAILED, result.stderr


def test_a_coloured_recap_still_names_its_host(tmp_path):
    """The interactive path forces ANSIBLE_FORCE_COLOR, so the host line arrives wrapped in
    escape codes; the check strips them before it reads the line."""
    coloured = (
        'echo "PLAY RECAP *********"; '
        r'printf "\033[0;32mdaniel-box\033[0m                 : \033[0;32mok=3   \033[0m '
        r'changed=0    unreachable=0    failed=0\n"'
    )
    result = _run_with_stubs(tmp_path, 0, recap=coloured)
    assert result.returncode == 0, result.stderr


def test_a_real_tag_miss_is_still_exit_2():
    """CLEAN half for the code the bug borrowed.

    Run without stubs: the tag validation happens before the lock and before any playbook, so
    this touches nothing. If exit 2 ever stopped meaning a tag miss, the fix above would have
    been a rename rather than a separation.

    `--skip-staleness-check` because the staleness check now runs FIRST (issue #1566): a
    checkout that happens to sit behind origin/master while this test runs would answer 4, and
    that would be the wrapper reporting correctly rather than the tag miss regressing. The
    ordering itself is pinned by test_deploy_staleness_precedes_tag_validation.py.
    """
    result = subprocess.run(
        [
            str(_DEPLOY_SH),
            "--tags",
            "definitely-not-a-real-service",
            "--skip-staleness-check",
        ],
        cwd=_REPO,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 2, result.stdout + result.stderr
