#!/usr/bin/env python3
"""Every `date` that stamps a deploy artifact or bounds a journal window reads UTC.

`deploy.sh`'s Python halves name its snapshot worktree and its `--detach` log after a stamp,
and `gitops_tick.sh` builds the `journalctl --since` window from a `date` one. A stamp in host-local time
is ambiguous across a DST fold and the window shifts with the host's zone; `-u` makes both
independent of where the script runs (issue #2154). Both hosts run `Etc/UTC` today, so this
guards a future zone change rather than a live defect.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_stamps_are_utc.py
"""

import re
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SCRIPTS = (_REPO / "scripts" / "deploy_tools" / "gitops_tick.sh",)
# A `date` invocation that formats or converts a time: `date +FMT`, `date -d @N +FMT`, and
# the `-u` form of each. Comment lines are skipped so prose naming the flag does not count.
# A flag's argument may not start with `-`, `'` or `+`, so each token has exactly one reading:
# an unrestricted `\S+` argument let `-a -b` parse as one flag or two, which backtracks
# exponentially on a long run of flags (CodeQL py/redos, alert #55).
_DATE_CALL = re.compile(r"\bdate\b((?:\s+-\S+(?:\s+[^\s'+-]\S*)?)*)\s+'?\+")
_DATE_UTC = re.compile(r"\bdate\s+-u\b")


def _date_calls(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if not line.lstrip().startswith("#") and _DATE_CALL.search(line)
    ]


@pytest.mark.parametrize("path", _SCRIPTS, ids=lambda p: p.name)
def test_every_date_stamp_carries_dash_u(path: Path):
    calls = _date_calls(path)
    # Non-vacuity: the snapshot stamp, the log stamp and the journal window are all `date`
    # calls, so an empty census means the pattern stopped matching, not that the file is clean.
    assert len(calls) >= 2, f"{path.name}: found no `date +` calls to check"
    without_u = [c for c in calls if not _DATE_UTC.search(c)]
    assert without_u == [], f"{path.name}: `date` without -u: {without_u}"


def test_the_pattern_flags_a_local_time_stamp():
    # The rejecting half: a stamp with no -u is what the guard exists to refuse.
    local = "stamp=$(date +%Y%m%d-%H%M%S)"
    assert _DATE_CALL.search(local)
    assert _DATE_CALL.search("""since="$(date '+%Y-%m-%d %H:%M:%S')\"""")
    assert not _DATE_UTC.search(local)
    # A flag with an argument still reaches the format.
    assert _DATE_CALL.search("""date -u -d "@$last_run" '+%F'""")


def test_the_pattern_stays_linear_on_a_run_of_flags():
    # The unambiguous argument class is the fix for the backtracking; 40 flags took over a
    # minute under the old pattern and take microseconds under this one.
    start = time.perf_counter()
    assert not _DATE_CALL.search("date" + " -!" * 40)
    assert time.perf_counter() - start < 1.0


# deploy.sh stamps its snapshot and its --detach log in Python since #2412.
_PYTHON_STAMPERS = (
    _REPO / "scripts" / "deploy_tools" / "deploy_under_locks.py",
    _REPO / "scripts" / "deploy_tools" / "deploy_detach.py",
)
_NOW_CALL = re.compile(r"\bdatetime\.now\(([^)]*)\)")


@pytest.mark.parametrize("path", _PYTHON_STAMPERS, ids=lambda p: p.name)
def test_every_python_stamp_is_utc(path: Path):
    calls = _NOW_CALL.findall(path.read_text())
    assert calls, f"{path.name}: found no datetime.now() call to check"
    assert all(arg == "UTC" for arg in calls), f"datetime.now() without UTC: {calls}"


def test_the_python_pattern_flags_a_local_time_stamp():
    assert _NOW_CALL.findall("datetime.now().strftime('%Y')") == [""]
