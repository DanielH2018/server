#!/usr/bin/env python3
"""The wait lines `deploy.sh` and `gitops_tick.sh` print, and that land.py books them.

`land_lib/landing.py:retry_while_locked` books a wait only when an attempt EXITS 75. Both
wrappers can wait a long time and exit 0 instead -- deploy.sh inside `flock -w LOCK_WAIT`,
gitops_tick.sh watching a tick another actor started -- so over the 14 days to 2026-09-11
every ledger row read `lock=0` while the tree lock was busy 17% of one day. These are the
lines that close that gap, asserted together with the parser that reads them: a wording
change on either side that the other does not follow is exactly the drift this file catches.

No test here touches /var/lock/server-git-tree.lock or the real systemd units. `flock`,
`fuser`, `ps`, `uv`, `systemctl` and `journalctl` are all stubbed on PATH, the way
test_deploy_exit_codes.py stubs `flock` and `uv`.

Run: uv run pytest scripts/deploy_tools/tests/test_wrapper_lock_wait_lines.py
"""

import os
import re
import subprocess
from pathlib import Path

from deploy_tools.land_lib import tools

_REPO = Path(__file__).resolve().parents[3]
_DEPLOY_SH = _REPO / "scripts" / "deploy.sh"
_TICK_SH = _REPO / "scripts" / "deploy_tools" / "gitops_tick.sh"

# `-n` is deploy.sh's uncontended probe and `-w` its timed acquire. Refusing the first and
# sleeping in the second is a held lock as far as the script can tell, with nothing held.
_FLOCK_CONTENDED = """#!/bin/bash
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

# `show <property>` expands to `systemctl show <unit> -p <property> --value`, so the property
# is $4 here. The monotonic stamp is derived from /proc/uptime, so the run reads as having
# started a known 300s ago whenever the test happens to run. ActiveState answers `activating`
# once and then takes two seconds to answer `inactive`, which is a tick that finished while
# this script was watching it.
_SYSTEMCTL = """#!/bin/bash
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
      ExecMainStartTimestampMonotonic)
        awk '{ printf "%d\\n", ($1 - 300) * 1000000 }' /proc/uptime ;;
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


def _stub_path(tmp_path: Path, stubs: dict[str, str]) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in stubs.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    return dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")


def _run_deploy(tmp_path: Path, flock: str) -> subprocess.CompletedProcess:
    env = _stub_path(tmp_path, {"flock": flock, "fuser": _FUSER, "ps": _PS, "uv": _UV})
    env["FUSER_STUB_SELF_FDS"] = str(tmp_path / "self-fds")
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


def test_joining_a_tick_in_flight_reports_how_long_it_ran_and_how_long_we_waited(
    tmp_path,
):
    """FLAGGED half: the join exits 0, so nothing else in the landing can see the wait."""
    env = _stub_path(tmp_path, {"systemctl": _SYSTEMCTL, "journalctl": _JOURNALCTL})
    env["TICK_STUB_STATE"] = str(tmp_path / "seen-activating")
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
    assert in_flight and 290 <= int(in_flight[1]) <= 320, line
    booked = tools.in_flock_wait(line)
    assert booked is not None, (
        f"land.py no longer parses gitops_tick.sh's line: {line!r}"
    )
    assert booked[0] >= 1
