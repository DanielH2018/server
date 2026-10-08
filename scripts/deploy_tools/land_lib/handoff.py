"""Hand a landing to a lander unit, for a user who cannot land a PR itself.

The `claude` agent user's login profile sets `LAND_HANDOFF_UNIT=claude-land`. That user's
GitHub token cannot merge and the user has no sudo, so a landing it ran itself would fail at the
merge or the deploy. Instead `land.sh` starts `<unit>@<pr>.service`, which runs land.sh as the
operator under the landing policy (`policy.py`), and reports that landing's result as its own:
the line the unit wrote to its `--verdict-file`, and land.sh's exit code as the unit recorded it.
`land.py` runs the handoff inside the same `--detach` fork as a landing, so
`land.sh ... --detach && cc-wait land <pr>` works unchanged for that user.

The unit always runs `--arm-merge --await-merge` and decides the deploy itself, so a flag that
would change what lands or what deploys is refused rather than dropped.
"""

import re
import subprocess
import sys as _sys
from collections.abc import Callable
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib.options import Options

# The unit's StateDirectory parent: `<unit>@.service` writes `/var/lib/<unit>/<pr>.verdict`.
STATE_ROOT = _Path("/var/lib")
# Every exit code land.sh documents, the set cc-wait's `land` source maps to a state. The unit
# can record others (a signal, flock giving up on its lock), which are reported as 1.
LAND_CODES = frozenset({0, 1, 64, 75})
# Above the unit's TimeoutStartSec, so systemd ends a stuck landing before this stops waiting.
START_TIMEOUT_S = 3 * 3600
# The PR numbers the lander's polkit rule lets the agent user start a unit for.
_PR_NUMBER = re.compile(r"[1-9][0-9]{0,6}")


def refused_flags(opts: Options) -> list[str]:
    """Each argument the unit cannot honour, as the flag the caller typed; empty when none."""
    refused = [
        flag
        for flag, value in (
            ("--tags", opts.tags),
            ("--since", opts.since),
            ("--subject", opts.subject),
        )
        if value
    ]
    if not _PR_NUMBER.fullmatch(opts.pr):
        refused.append(f"--pr {opts.pr} (not a PR number)")
    return refused


def _mtime(path: _Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _status(
    systemctl: Callable[..., subprocess.CompletedProcess[str]], unit: str
) -> dict:
    shown = systemctl("show", "-p", "Result", "-p", "ExecMainStatus", unit, timeout=30)
    return dict(line.split("=", 1) for line in shown.stdout.splitlines() if "=" in line)


def land_through_unit(
    pr: str,
    unit: str,
    systemctl: Callable[..., subprocess.CompletedProcess[str]],
    state_root: _Path = STATE_ROOT,
) -> int:
    """Start `<unit>@<pr>.service`, wait for it, and report its landing; land.sh's exit code.

    Prints the unit's `VERDICT:` line to stdout, or `land: <why>` to stderr when the landing
    stopped without one. A verdict file the unit did not write during this start is an earlier
    landing's, and is never reported as this one's.
    """
    instance = f"{unit}@{pr}.service"
    verdict = state_root / unit / f"{pr}.verdict"
    before = _mtime(verdict)
    print(
        f"== handing PR #{pr} to {instance}, which lands it as the operator", flush=True
    )
    try:
        started = systemctl("start", instance, timeout=START_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        print(
            f"land: gave up waiting on {instance} after {START_TIMEOUT_S // 3600}h; it may "
            f"still be running (systemctl status {instance}), and a re-run waits on it again",
            file=_sys.stderr,
        )
        return 75
    after = _mtime(verdict)
    line = verdict.read_text().strip() if after is not None and after != before else ""
    if not line or line == "PENDING":
        why = started.stderr.strip() or (
            f"Result={_status(systemctl, instance).get('Result') or '<unknown>'}"
        )
        print(
            f"land: {instance} ended without a verdict ({why}); journalctl -u {instance}",
            file=_sys.stderr,
        )
        return 1
    stopped = re.fullmatch(r"STOPPED: rc=(\d+) (.*)", line, re.DOTALL)
    if stopped:
        print(f"land: {stopped.group(2)}", file=_sys.stderr)
        rc = int(stopped.group(1))
        return rc if rc in LAND_CODES else 1
    print(line)
    recorded = _status(systemctl, instance).get("ExecMainStatus", "")
    rc = int(recorded) if recorded.isdigit() else 1
    return rc if rc in LAND_CODES else 1
