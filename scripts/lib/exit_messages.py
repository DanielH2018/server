"""What an entry point prints about its own exit code, read from `lib.exit_codes.CONTRACTS`.

`describe` and `die` print a failing code's name, meaning and remedy, and `verdict_line`
prints the one-token verdict line a landing greps for. They sit beside `lib/exit_codes.py`
rather than in it so that module holds the codes and their rows alone. The import runs one
way, from here to there, so a family added to `exit_codes` never has to import this module
back.

Typical usage example:

    from lib.exit_messages import die

    die("scripts/deploy.sh", DEPLOY_STALE, "the checkout is behind origin")
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import sys
from pathlib import PurePosixPath
from typing import NoReturn

from lib.exit_codes import contract


# -- the verdict line an entry point ends with ------------------------------------------
#
# `land.sh` prints `VERDICT: <verdict> (<detail>)` as its last line. `fanout_status.py` and the
# `land-after-merge` wait both find a landing's outcome by grepping `^VERDICT:`.
#
# DECIDED: deploy.sh and the tick print that same shape under their OWN prefix rather than
# under `VERDICT:`. A landing runs both as subprocesses with stdout INHERITED
# (`land_lib/tools.py:run_deploy`), so their lines reach the landing's own logfile ABOVE its
# verdict -- and `grep -m1 '^VERDICT:'` takes the FIRST match, which would report the deploy's
# outcome as the landing's on every landing, with nothing failing. One format, three prefixes,
# one renderer. Issue #2853 asked for a bare `VERDICT:` from all three; the PR that closed it
# carries the measurement.
VERDICT_PREFIXES = {
    "scripts/deploy.sh": "DEPLOY-VERDICT",
    "scripts/deploy_tools/gitops_tick.sh": "TICK-VERDICT",
    "scripts/deploy_tools/land.sh": "VERDICT",
    # A landing runs `probe.py health` and its stdout can reach the landing's log, so the probe
    # takes its own prefix for the reason the DECIDED note above gives.
    "scripts/diagnostics/probe.py": "PROBE-VERDICT",
}


def verdict_line(entry_point: str, rc: int, detail: str) -> str | None:
    """`<PREFIX>: <verdict> (<detail>)` for `rc`, or None when the code names no verdict.

    The outermost tool an operator ran is the one whose prefix is bare `VERDICT`; see the
    DECIDED note above for why the inner two are not.
    """
    prefix = VERDICT_PREFIXES.get(entry_point)
    for code in contract(entry_point):
        if code.value == rc and prefix and code.verdict:
            return f"{prefix}: {code.verdict} ({detail})"
    return None


def describe(entry_point: str, rc: int) -> str | None:
    """`<const> (<value>): <meaning> <remedy>` for `rc`, or None when it is not in the contract.

    What an entry point prints on its own non-zero exit, so an operator reading a bare
    `Exit code N` does not have to look the number up. The wrapper prints this; nothing
    downstream re-decodes it.
    """
    for code in contract(entry_point):
        if code.value == rc:
            tail = f" {code.remedy}" if code.remedy else ""
            return f"{code.const} ({code.value}): {code.meaning}{tail}"
    return None


def die(entry_point: str, rc: int, detail: str) -> NoReturn:
    """Print `<prog>: error: <detail>` and the code's meaning to stderr, then exit `rc`.

    `<prog>` is the entry point's file name. The second line is `describe(entry_point, rc)`,
    left out when the contract has no row for `rc`.

    Raises:
        SystemExit: always, with `rc` as its code.
    """
    print(f"{PurePosixPath(entry_point).name}: error: {detail}", file=sys.stderr)
    note = describe(entry_point, rc)
    if note:
        print(note, file=sys.stderr)
    raise SystemExit(rc)
