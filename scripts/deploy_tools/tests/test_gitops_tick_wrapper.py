#!/usr/bin/env python3
"""`gitops_tick.sh` against a stubbed systemd: joining, kicking, and the fresh run after a join.

The wrapper is a shell script, so the pytest suite cannot see it except by running it. Every
`systemctl` and `journalctl` here is a stub on PATH; nothing touches the real unit, the real
journal, or /var/lock. The only live reads are the wrapper's own `/var/lib/gitops-deploy`
markers, which it prints and never grades.

The lines the wrapper prints for a landing to book -- `gitops_tick: joined ...` -- are
asserted together with the parser that reads them (`land_lib/tools.py:in_flock_wait`), so a
wording change on either side that the other does not follow is caught here.

Run: uv run pytest scripts/deploy_tools/tests/test_gitops_tick_wrapper.py
"""

import re
import subprocess
from pathlib import Path

from _deploy_sh_fakes import stub_path as _stub_path
from deploy_tools import exit_codes as ec
from deploy_tools.land_lib import tools

_REPO = Path(__file__).resolve().parents[3]
_TICK_SH = _REPO / "scripts" / "deploy_tools" / "gitops_tick.sh"

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
# is $4 here. ActiveState answers `activating` once and then `inactive`, which is a tick that
# finished while this script was watching it. A joined run that ends cleanly is followed by a
# FRESH run (issue #1879), so once `start` has been asked the stub runs a second activation
# under a NEW monotonic stamp -- what the watch loop needs to see that run finish. `sleep 2`
# on the first `inactive` is the joined run taking a moment to end.
_NEW_MONOTONIC_US = _MONOTONIC_US + 60 * 1_000_000

_SYSTEMCTL = f"""#!/bin/bash
case "$1" in
  cat) exit 0 ;;
  start) echo start >>"$TICK_STUB_STARTED"; exit 0 ;;
  show)
    case "$4" in
      ActiveState)
        if [[ -e "$TICK_STUB_STARTED" ]]; then
          if [[ -e "$TICK_STUB_STATE.fresh" ]]; then echo inactive
          else : >"$TICK_STUB_STATE.fresh"; echo activating; fi
        elif [[ -e "$TICK_STUB_STATE" ]]; then
          sleep 2
          echo inactive
        else : >"$TICK_STUB_STATE"; echo activating; fi
        ;;
      ExecMainStartTimestampMonotonic)
        if [[ -e "$TICK_STUB_STATE.fresh" ]]; then echo {_NEW_MONOTONIC_US}
        else echo {_MONOTONIC_US}; fi
        ;;
      ExecMainStartTimestamp) echo "Thu 2026-09-11 10:00:00 CDT" ;;
      Result) echo "${{TICK_STUB_RESULT:-success}}" ;;
      ExecMainStatus) echo "${{TICK_STUB_STATUS:-0}}" ;;
      *) echo "" ;;
    esac ;;
esac
exit 0
"""

_JOURNALCTL = """#!/bin/bash
exit 0
"""


def test_joining_a_tick_in_flight_reports_how_long_it_ran_and_how_long_we_waited(
    tmp_path,
):
    """FLAGGED half: the join exits 0, so nothing else in the landing can see the wait."""
    env = _stub_path(tmp_path, {"systemctl": _SYSTEMCTL, "journalctl": _JOURNALCTL})
    env["TICK_STUB_STATE"] = str(tmp_path / "seen-activating")
    env["TICK_STUB_STARTED"] = str(tmp_path / "started")
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


# `systemctl show` answers `activating` forever: a run in flight that does not end while the
# script looks. `start` records that it was asked, which is the thing the join must not do.
_SYSTEMCTL_IN_FLIGHT = f"""#!/bin/bash
case "$1" in
  cat) exit 0 ;;
  start) : >"$TICK_STUB_STARTED"; exit 0 ;;
  show)
    case "$4" in
      ActiveState) echo activating ;;
      ExecMainStartTimestampMonotonic) echo {_MONOTONIC_US} ;;
      ExecMainStartTimestamp) echo "Thu 2026-09-11 10:00:00 CDT" ;;
      *) echo "" ;;
    esac ;;
esac
exit 0
"""

_SYSTEMCTL_IDLE = _SYSTEMCTL_IN_FLIGHT.replace("echo activating", "echo inactive")


def _kick(tmp_path: Path, systemctl: str) -> tuple[subprocess.CompletedProcess, bool]:
    """`gitops_tick.sh --no-wait` against a stub; (result, whether `start` was asked)."""
    env = _stub_path(tmp_path, {"systemctl": systemctl, "journalctl": _JOURNALCTL})
    started = tmp_path / "started"
    env["TICK_STUB_STARTED"] = str(started)
    uptime = tmp_path / "uptime"
    uptime.write_text(_UPTIME_FIXTURE)
    env["GITOPS_TICK_UPTIME_SOURCE"] = str(uptime)
    result = subprocess.run(
        [str(_TICK_SH), "--no-wait"],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result, started.exists()


def test_a_no_wait_kick_that_joins_a_run_in_flight_exits_4_and_starts_nothing(tmp_path):
    """Issue #1843: the run in flight fetched before the caller's commit merged, so exit 0
    here told land.sh the primary was converging when nothing would converge it."""
    result, started = _kick(tmp_path, _SYSTEMCTL_IN_FLIGHT)
    assert result.returncode == ec.TICK_JOINED, result.stdout
    assert not started, "the join must not issue a second `systemctl start`"
    assert "Nothing started" in result.stdout
    assert f"already {_IN_FLIGHT_S}s" in result.stdout


def test_a_no_wait_kick_on_an_idle_unit_starts_one_and_exits_0(tmp_path):
    """CLEAN half: with no run in flight the request starts a tick, as before."""
    result, started = _kick(tmp_path, _SYSTEMCTL_IDLE)
    assert result.returncode == ec.TICK_OK, result.stdout
    assert started
    assert "Started." in result.stdout


# `-w` answering 75 is a real timeout (deploy.sh passes `-E "$LOCK_BUSY"`); `-w` answering 1
# is any OTHER flock failure, which must not be reported as contention. Measured 2026-09-11

# ── issue #1879: a joined run in WAIT mode is followed by a fresh one ─────────────────────────
# The joined run fetched origin before this request arrived, so grading it grades a tick that
# never carried the caller's commit. `_SYSTEMCTL` above runs the second activation.
_JOURNALCTL_CONTENDED = """#!/bin/bash
echo "gitops-deploy: tick skipped (lock contention) — held for the full flock wait"
"""


def _wait_on_joined(
    tmp_path: Path,
    journalctl: str = _JOURNALCTL,
    result: str = "success",
    status: str = "0",
) -> tuple[subprocess.CompletedProcess, int]:
    """`gitops_tick.sh` (wait mode) against a run already in flight; (result, starts asked)."""
    env = _stub_path(tmp_path, {"systemctl": _SYSTEMCTL, "journalctl": journalctl})
    started = tmp_path / "started"
    env["TICK_STUB_STARTED"] = str(started)
    env["TICK_STUB_STATE"] = str(tmp_path / "seen-activating")
    env["TICK_STUB_RESULT"] = result
    env["TICK_STUB_STATUS"] = status
    uptime = tmp_path / "uptime"
    uptime.write_text(_UPTIME_FIXTURE)
    env["GITOPS_TICK_UPTIME_SOURCE"] = str(uptime)
    proc = subprocess.run(
        [str(_TICK_SH), "--wait", "60"],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    starts = len(started.read_text().splitlines()) if started.exists() else 0
    return proc, starts


def test_a_joined_run_that_ends_cleanly_is_followed_by_a_fresh_run(tmp_path):
    """FLAGGED half (#1879): the joined run is not this request's tick, so one is started.

    The joined run fetched before the request; grading it left `land.sh` reading `deferred`
    or `needs-manual-apply` off markers a tick a minute later cleared. The fresh run is
    started only after the joined one ENDED, and the wrapper exits by the fresh run's
    outcome.
    """
    proc, starts = _wait_on_joined(tmp_path)
    assert proc.returncode == ec.TICK_OK, proc.stdout + proc.stderr
    assert starts == 1, (
        "exactly one `systemctl start`: none for the join, one fresh run"
    )
    assert "started and graded instead" in proc.stdout
    stderr = proc.stderr.splitlines()
    joined = next((x for x in stderr if "gitops_tick: joined a tick" in x), "")
    assert tools.in_flock_wait(joined), "the joined line the landing books must survive"
    assert any("started a fresh run" in x for x in stderr), stderr


def test_a_joined_run_that_failed_is_graded_as_itself(tmp_path):
    """CLEAN half: a failed joined run exits 1 and starts nothing.

    A fresh run after a failure skips on the hold the failed one wrote and would read green
    over a fault the caller has to see.
    """
    proc, starts = _wait_on_joined(tmp_path, result="failed", status="1")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert starts == 0, "a failed joined run must not be followed by a fresh one"


def test_a_joined_run_that_hit_contention_is_graded_as_itself(tmp_path):
    """CLEAN half: contention exits 3 and starts nothing; the caller's retry loop owns it."""
    proc, starts = _wait_on_joined(tmp_path, journalctl=_JOURNALCTL_CONTENDED)
    assert proc.returncode == ec.TICK_LOCK_CONTENTION, proc.stdout + proc.stderr
    assert starts == 0
