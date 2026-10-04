#!/usr/bin/env python3
"""The `land` source for cc-wait: one detached landing's state, read from its log.

`cc-wait land <pr>` runs this through `.claude/wait-sources/land`. cc-wait owns the loop, the
budget and the output; this answers one question per call: where is this landing now?

    land_probe.py --describe <pr> [--log PATH | --log-dir DIR]   # terminal states, as JSON
    land_probe.py <pr> [--log PATH | --log-dir DIR]              # the state now, one JSON line

The contract is the dotfiles repo's docs/specs/2026-10-04-cc-wait-design.md.

WHICH LANDING. `--log` names it exactly, and `land.sh --detach` prints the command with it.
Without `--log`, the newest `land<pr>-*.log` in `--log-dir` (default: where `land.sh --detach`
writes, `detach.log_path`'s directory) is the landing. `land.sh --detach` creates the log before
it returns, so a wait chained after it never reads an earlier landing of the same PR.

THE EXIT CODE IS THE AUTHORITY, as it is for `detach.py`: a recorded `.rc` ends the wait with
land.sh's own code, and the `VERDICT:` line is the detail. The one exception is 75. land.sh
exits 75 for "gave up waiting; re-run land.sh", but cc-wait keeps 75 for "re-run this wait", so
a landing that gave up ends the wait with 3 instead, and its verdict line says which give-up it
was.
"""

import argparse
import json
import re
import sys
from pathlib import Path

from land_lib import detach

# land.sh's exit code -> the state that ends the wait. Every code land.sh documents is here.
_STATE_BY_RC = {0: "landed", 1: "failed", 64: "bad-arguments", 75: "gave-up"}

# Each terminal state -> the exit code cc-wait ends with. `gave-up` is land.sh's 75, remapped.
TERMINAL = {"landed": 0, "failed": 1, "bad-arguments": 64, "gave-up": 3, "died": 1}

# A landing's phase lines: `== 4/6  deploying ...`. The newest one is the progress detail.
_PHASE = re.compile(r"^== (.+)$", re.MULTILINE)


class NoLanding(Exception):
    """No log exists for the landing the arguments name."""


def find_log(pr: str, log: str, log_dir: str) -> Path:
    """The log of the landing the arguments name.

    Raises:
      NoLanding: `--log` names a missing file, or `--log-dir` holds no log for `pr`.
    """
    if log:
        path = Path(log)
        if not path.exists():
            raise NoLanding(f"{path} does not exist")
        return path
    base = Path(log_dir) if log_dir else detach.default_log_dir()
    logs = sorted(base.glob(f"land{pr}-*.log"))
    if not logs:
        raise NoLanding(f"no land{pr}-*.log in {base}")
    return logs[-1]


def describe(log: Path) -> dict:
    return {"terminal": TERMINAL, "watch": [str(log.parent)], "interval_s": 10}


def read(log: Path) -> dict:
    """The landing's state: terminal once it recorded a code, or once it died without one."""
    code = detach.recorded_code(log)
    if code is None:
        pid = detach.recorded_pid(log)
        # The landing writes its code and then exits, so the code is read again after a pid
        # that is gone: only a landing killed mid-run leaves neither.
        if pid is not None and not detach.alive(pid):
            code = detach.recorded_code(log)
            if code is None:
                return {
                    "state": "died",
                    "detail": f"the landing (pid {pid}) died without recording an exit code; "
                    f"read {log}",
                }
    if code is not None:
        verdict = detach.verdict_in(log) or f"no VERDICT line; read {log}"
        state = _STATE_BY_RC.get(code, "failed")
        detail = verdict if code in _STATE_BY_RC else f"exit code {code}: {verdict}"
        return {"state": state, "detail": detail}
    phases = _PHASE.findall(log.read_text(errors="replace"))
    return {
        "state": "running",
        "detail": " ".join(phases[-1].split()) if phases else str(log),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="land_probe.py", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--describe", action="store_true")
    parser.add_argument("pr")
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--log", default="")
    where.add_argument("--log-dir", default="")
    ns = parser.parse_args(argv)
    try:
        log = find_log(ns.pr, ns.log, ns.log_dir)
    except NoLanding as exc:
        print(f"land_probe: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(describe(log) if ns.describe else read(log)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
