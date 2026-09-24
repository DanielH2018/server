#!/usr/bin/env python3
"""Every `date` that stamps a deploy artifact or bounds a journal window reads UTC.

`deploy_locked.sh` (the locked half behind `deploy.sh`) names its snapshot worktree and its log after a `date` stamp, and
`gitops_tick.sh` builds the `journalctl --since` window from one. A stamp in host-local time
is ambiguous across a DST fold and the window shifts with the host's zone; `-u` makes both
independent of where the script runs (issue #2154). Both hosts run `Etc/UTC` today, so this
guards a future zone change rather than a live defect.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_stamps_are_utc.py
"""

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SCRIPTS = (
    _REPO / "scripts" / "deploy_tools" / "deploy_locked.sh",
    _REPO / "scripts" / "deploy_tools" / "gitops_tick.sh",
)
# A `date` invocation that formats or converts a time: `date +FMT`, `date -d @N +FMT`, and
# the `-u` form of each. Comment lines are skipped so prose naming the flag does not count.
_DATE_CALL = re.compile(r"\bdate\b((?:\s+-\S+(?:\s+\S+)?)*)\s+'?\+")
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
