#!/usr/bin/env python3
"""The wait lines `deploy.sh` and `gitops_tick.sh` print, and that land.py books them.

`land_lib/landing.py:retry_while_locked` books a wait only when an attempt EXITS 75. Both
wrappers can wait a long time and exit 0 instead -- deploy.sh inside `flock -w LOCK_WAIT`,
gitops_tick.sh watching a tick another actor started -- so over the 14 days to 2026-09-11
every ledger row read `lock=0` while the tree lock was busy 17% of one day. These are the
lines that close that gap, asserted together with the parser that reads them: a wording
change on either side that the other does not follow is exactly the drift this file catches.

No test here touches /var/lock/server-git-tree.lock, the real systemd units, or the host's
syslog. `flock`, `fuser`, `ps`, `uv`, `logger`, `systemctl` and `journalctl` are all stubbed
on PATH, the way test_deploy_exit_codes.py stubs `flock` and `uv`. The only live reads are
gitops_tick.sh's own `/var/lib/gitops-deploy` markers.

Run: uv run pytest scripts/deploy_tools/tests/test_wrapper_lock_wait_lines.py
"""

import os
import re
import subprocess
from pathlib import Path

from _deploy_sh_fakes import deploy_sh_env, make_snapshot_repo
from deploy_tools import exit_codes as ec
from deploy_tools.land_lib import tools

_REPO = Path(__file__).resolve().parents[3]
_DEPLOY_SH = _REPO / "scripts" / "deploy.sh"
_TICK_SH = _REPO / "scripts" / "deploy_tools" / "gitops_tick.sh"


def _deploy_repo_env(tmp_path: Path, bin_dir: Path) -> tuple[Path, dict[str, str]]:
    """A throwaway repo for deploy.sh to snapshot, and the env that keeps the run inside it.

    deploy.sh now copies HEAD into a detached worktree before it runs the playbook. Run
    against this checkout, every test here would register a real worktree under the real
    `.git`; `git_free_env` is what stops a prek hook's `GIT_DIR` overriding `cwd` and doing
    that anyway. The snapshot root and the per-service lock directory are redirected for the
    same reason: nothing here may write under /var/lock or /tmp/homelab-deploy-snapshots.
    """
    return make_snapshot_repo(tmp_path / "repo"), deploy_sh_env(tmp_path, bin_dir)


# deploy.sh's three flock shapes, and only two of them are the tree lock: `-n <fd>` is its
# uncontended probe, `-w` its timed acquire, and `-n -x <fd>` the snapshot's own owner lock,
# which nothing here contends. Refusing the probe and sleeping in the acquire is a held tree
# lock as far as the script can tell, with nothing actually held.
_FLOCK_CONTENDED = """#!/bin/bash
case "$1 $2" in
  "-n -x") exit 0 ;;
esac
case "$1" in
  -n) exit 1 ;;
  -w) sleep 1; exit 0 ;;
esac
exit 0
"""

_FLOCK_FREE = """#!/bin/bash
exit 0
"""

# Records how many descriptors the CALLER already has on the lock file, which is the thing
# real `fuser` would have reported as a holder. Measured on 2026-09-11: `fuser` scans every
# process's descriptors, so a deploy.sh that opens the lock before sampling reports itself --
# and `fuser "$LOCK" {fd}>&-` does not help, because the parent shell still holds it.
_FUSER = """#!/bin/bash
ls -l "/proc/$PPID/fd" 2>/dev/null |
  awk '/server-git-tree.lock/ { n++ } END { print n + 0 }' >"$FUSER_STUB_SELF_FDS"
echo "  4242"
"""

_PS = """#!/bin/bash
echo "   99 uv run ansible-playbook ansible/deploy.yml --tags sonarr"
"""

_UV = """#!/bin/bash
exit 0
"""

# A fixed boot clock, handed to gitops_tick.sh through GITOPS_TICK_UPTIME_SOURCE, so the
# arithmetic under test has no dependence on how long THIS machine has been up. Deriving the
# stamp from the real /proc/uptime cannot work: on a runner whose uptime is under
# `_IN_FLIGHT_S`, `uptime - 300` is negative, the script's `^[0-9]+$` guard rejects it, and
# the test silently lands in the `${seconds:-0}` fallback instead of the arithmetic it exists
# to check. That is how PR #1769 read `already 0s in flight` on CI and 300s here.
# Whole seconds so the subtraction is exact in floating point.
_UPTIME_S = 123456
_IN_FLIGHT_S = 300
_UPTIME_FIXTURE = f"{_UPTIME_S}.00 98765.43\n"
_MONOTONIC_US = (_UPTIME_S - _IN_FLIGHT_S) * 1_000_000

# `show <property>` expands to `systemctl show <unit> -p <property> --value`, so the property
# is $4 here. ActiveState answers `activating` once and then takes two seconds to answer
# `inactive`, which is a tick that finished while this script was watching it.
_SYSTEMCTL = f"""#!/bin/bash
case "$1" in
  cat) exit 0 ;;
  show)
    case "$4" in
      ActiveState)
        if [[ -e "$TICK_STUB_STATE" ]]; then
          sleep 2
          echo inactive
        else
          : >"$TICK_STUB_STATE"
          echo activating
        fi
        ;;
      ExecMainStartTimestampMonotonic) echo {_MONOTONIC_US} ;;
      ExecMainStartTimestamp) echo "Thu 2026-09-11 10:00:00 CDT" ;;
      Result) echo success ;;
      ExecMainStatus) echo 0 ;;
      *) echo "" ;;
    esac ;;
esac
exit 0
"""

_JOURNALCTL = """#!/bin/bash
exit 0
"""

# `_UV` exits 0 for the playbook, so deploy.sh reaches `emit_deploy_annotation`, which writes
# an `event=deploy` line through `logger`. conftest's autouse `_no_syslog` already intercepts
# that directory-wide, and measurement confirms no test run reached /var/log/syslog. This stub
# is here so the property does not depend on a fixture in another file: a test that writes to
# the host's syslog lands on the real Deploys board beside real deploys.
_LOGGER = """#!/bin/bash
exit 0
"""


def _stub_bin(tmp_path: Path, stubs: dict[str, str]) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in stubs.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    return bin_dir


def _stub_path(tmp_path: Path, stubs: dict[str, str]) -> dict[str, str]:
    bin_dir = _stub_bin(tmp_path, stubs)
    return dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")


def _run_deploy(tmp_path: Path, flock: str) -> subprocess.CompletedProcess:
    bin_dir = _stub_bin(
        tmp_path,
        {
            "flock": flock,
            "fuser": _FUSER,
            "ps": _PS,
            "uv": _UV,
            "logger": _LOGGER,
        },
    )
    repo, env = _deploy_repo_env(tmp_path, bin_dir)
    env["FUSER_STUB_SELF_FDS"] = str(tmp_path / "self-fds")
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


def test_a_contended_acquire_is_reported_with_its_seconds_and_its_holder(tmp_path):
    """FLAGGED half: the wait deploy.sh rides out inside `flock -w` must reach the log."""
    result = _run_deploy(tmp_path, _FLOCK_CONTENDED)
    assert result.returncode == 0, result.stderr
    line = next((x for x in result.stderr.splitlines() if "lock acquired" in x), "")
    assert line, result.stderr
    assert "(holder was: pid 4242" in line
    assert "ansible-playbook ansible/deploy.yml --tags sonarr" in line
    booked = tools.in_flock_wait(line)
    assert booked is not None, f"land.py no longer parses deploy.sh's line: {line!r}"
    assert booked[0] >= 1
    assert (tmp_path / "self-fds").read_text().strip() == "0", (
        "deploy.sh held the lock file when it sampled the holder, so real `fuser` would "
        "have reported deploy.sh itself and the landing would name itself as its blocker"
    )


def test_an_uncontended_acquire_says_nothing(tmp_path):
    """CLEAN half: a line on every deploy would make `lock=0` rows unreadable as evidence."""
    result = _run_deploy(tmp_path, _FLOCK_FREE)
    assert result.returncode == 0, result.stderr
    assert "lock acquired" not in result.stderr
    assert "service lock" not in result.stderr


# The tree lock is free (`-n` succeeds at once) and the per-tag lock is not: `-w` sleeps before
# it grants. That is a deploy of the SAME service already running, which is the only thing a
# service lock ever waits for, and the wait that used to be invisible because the tree lock had
# already been paid by then.
_FLOCK_SERVICE_CONTENDED = """#!/bin/bash
case "$1" in
  -n) exit 0 ;;
  -s) exit 0 ;;
  -w) sleep 1; exit 0 ;;
esac
exit 0
"""


def test_a_contended_service_lock_reports_its_tag_and_its_seconds(tmp_path):
    """FLAGGED half: most of what a landing waits for is now THIS lock, not the tree lock.

    Without this line a landing behind another deploy of the same service books `lock=0` and
    charges the wait to `deploy` — the same gap the tree-lock line closed for the tree lock.
    """
    result = _run_deploy(tmp_path, _FLOCK_SERVICE_CONTENDED)
    assert result.returncode == 0, result.stderr
    line = next((x for x in result.stderr.splitlines() if "service lock" in x), "")
    assert line, result.stderr
    assert "service lock uptime-kuma acquired after" in line
    booked = tools.in_flock_wait(line)
    assert booked is not None, f"land.py no longer parses deploy.sh's line: {line!r}"
    assert booked[0] >= 1
    # The tree lock was free, so nothing may be booked against it.
    assert "deploy: lock acquired after" not in result.stderr


def test_the_service_lock_refusal_is_not_booked_as_a_wait():
    """CLEAN half for the parser itself: only an ACQUIRE reports time this landing waited.

    The refusal line names ${LOCK_WAIT} seconds. A pattern loose enough to match it would book
    the whole budget onto a landing that deployed nothing, which is the opposite of what
    `lock=` means.
    """
    refusal = "deploy: a service lock under /var/lock stayed busy for 3000s -- nothing"
    assert tools.in_flock_wait(refusal) is None
    assert (
        tools.in_flock_wait("deploy: service lock sonarr acquired after 4s ok") is None
    )


def test_joining_a_tick_in_flight_reports_how_long_it_ran_and_how_long_we_waited(
    tmp_path,
):
    """FLAGGED half: the join exits 0, so nothing else in the landing can see the wait."""
    env = _stub_path(tmp_path, {"systemctl": _SYSTEMCTL, "journalctl": _JOURNALCTL})
    env["TICK_STUB_STATE"] = str(tmp_path / "seen-activating")
    uptime = tmp_path / "uptime"
    uptime.write_text(_UPTIME_FIXTURE)
    env["GITOPS_TICK_UPTIME_SOURCE"] = str(uptime)
    result = subprocess.run(
        [str(_TICK_SH)],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    line = next(
        (x for x in result.stderr.splitlines() if "gitops_tick: joined" in x), ""
    )
    assert line, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    in_flight = re.search(r"already (\d+)s in flight", line)
    # Exact, not a window: both operands are fixed, so any drift is a real arithmetic change.
    # `!= 0` is the load-bearing part -- 0 is what the `${seconds:-0}` fallback produces, and a
    # test that accepted it would pass while checking none of the conversion.
    assert in_flight and int(in_flight[1]) == _IN_FLIGHT_S, line
    booked = tools.in_flock_wait(line)
    assert booked is not None, (
        f"land.py no longer parses gitops_tick.sh's line: {line!r}"
    )
    assert booked[0] >= 1


# `-w` answering 75 is a real timeout (deploy.sh passes `-E "$LOCK_BUSY"`); `-w` answering 1
# is any OTHER flock failure, which must not be reported as contention. Measured 2026-09-11
# against real flock on a descriptor: a timeout with `-E 75` exits 75, without it exits 1, and
# a bad descriptor exits 65 — so the flag is what keeps the two apart.
_FLOCK_TIMES_OUT = """#!/bin/bash
case "$1" in
  -n) exit 1 ;;
  -w) exit 75 ;;
esac
exit 0
"""

_FLOCK_ERRORS = """#!/bin/bash
case "$1" in
  -n) exit 1 ;;
  -w) echo "flock: bad things" >&2; exit 1 ;;
esac
exit 0
"""


def test_a_lock_timeout_is_still_reported_as_contention(tmp_path):
    """CLEAN half for exit 75: the wait really did elapse, so nothing was deployed."""
    result = _run_deploy(tmp_path, _FLOCK_TIMES_OUT)
    assert result.returncode == 75, result.stderr
    assert "nothing was deployed" in result.stderr


def test_any_other_flock_failure_is_not_reported_as_contention(tmp_path):
    """FLAGGED half: dropping `-E` made every flock failure read as a busy lock.

    That tells an operator "a deploy is already running, retry shortly" for a lock file the
    wrapper could not open at all, which is a resume point that never resumes.
    """
    result = _run_deploy(tmp_path, _FLOCK_ERRORS)
    assert result.returncode != 75, result.stderr
    assert "A deploy is already running" not in result.stderr


def test_a_flock_failure_that_is_not_contention_exits_its_own_code(tmp_path):
    """FLAGGED half for issue #1775: it used to fall through to 20.

    20 promises "a task failed AFTER applying; some changes are live", which land.py prints
    verbatim — for a run that never started ansible. 76 says what happened instead.
    """
    result = _run_deploy(tmp_path, _FLOCK_ERRORS)
    assert result.returncode == ec.DEPLOY_LOCK_UNAVAILABLE, result.stderr
    assert "flock exit 1" in result.stderr
    assert "the playbook ran and failed" not in result.stderr
    assert ec.DEPLOY_LOCK_UNAVAILABLE in ec.DEPLOY_SH_NO_VERDICT


# `--detach` takes the same lock through a second, non-blocking call, so it needs both halves
# of the same pair. Its `flock -n` carries `-E "$LOCK_BUSY"`, so a held lock answers 75 here.
_FLOCK_DETACH_BUSY = """#!/bin/bash
case "$1 $2" in
  "-n -E") exit 75 ;;
esac
exit 0
"""

_FLOCK_DETACH_ERRORS = """#!/bin/bash
case "$1 $2" in
  "-n -E") echo "flock: bad file descriptor" >&2; exit 65 ;;
esac
exit 0
"""


def _run_detach(tmp_path: Path, flock: str) -> subprocess.CompletedProcess:
    bin_dir = _stub_bin(
        tmp_path,
        {"flock": flock, "fuser": _FUSER, "ps": _PS, "uv": _UV, "logger": _LOGGER},
    )
    repo, env = _deploy_repo_env(tmp_path, bin_dir)
    env["FUSER_STUB_SELF_FDS"] = str(tmp_path / "self-fds")
    return subprocess.run(
        [
            str(_DEPLOY_SH),
            "--detach",
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


def test_detach_still_reports_a_held_lock_as_contention(tmp_path):
    """CLEAN half: --detach fails fast on a real holder, and 75 says retry shortly."""
    result = _run_detach(tmp_path, _FLOCK_DETACH_BUSY)
    assert result.returncode == ec.DEPLOY_LOCK_BUSY, result.stderr
    assert "A deploy is already running" in result.stderr


def test_detach_tells_a_broken_lock_file_from_a_held_one(tmp_path):
    """FLAGGED half: `flock -n` without `-E` answers 1 for both, and this arm read both
    as contention."""
    result = _run_detach(tmp_path, _FLOCK_DETACH_ERRORS)
    assert result.returncode == ec.DEPLOY_LOCK_UNAVAILABLE, result.stderr
    assert "flock exit 65" in result.stderr
    assert "A deploy is already running" not in result.stderr
