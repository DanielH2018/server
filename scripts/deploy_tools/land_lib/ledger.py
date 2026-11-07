"""What the Landings board reads: one logfmt line per landing, from a Ledger of stamps.

This is the only record of where a landing's time goes: how long the merge took to arrive,
how long master CI took after it, the tick, the deploy.

`tick`, `lock`, `holder` and `kick` stay on the line so the Landings board keeps its columns,
but land.sh runs no tick: `tick` reads 0, `lock` reads 0, and `holder` and `kick` are empty.

`cause` is a one-token reason beside a `deploy-failed` verdict. One exit-75 verdict carries one
too: `deferred` with `promote-timeout`, the wait on a promotion that no promotion covered in
time. Every other verdict leaves it empty. The Landings board's cause panel selects `cause!=""`,
so it counts that one beside the `deploy-failed` causes (#4998). The verdict alone cannot tell
a tag nothing applies from a failed tick or a held promotion, so `cause` lets the board split
them. Its vocabulary is `outcome.Cause` and it is checked HERE, on assignment,
because the board groups by the field: a value invented at one of the seven writing sites
becomes a bar on the dashboard, and nothing downstream would reject it.
"""

from dataclasses import dataclass

import sys
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib.outcome import CAUSES, Cause
from lib.repo_paths import HOST_LIB_FILES

_sys.path.insert(0, str(HOST_LIB_FILES))
from host_lib import logfmt


@dataclass
class Ledger:
    """The stamps and labels the annotation is built from. Phases write; annotation reads."""

    pr: str
    t_start: float
    t_merged: float | None = None
    t_ci: float | None = None
    t_tick: float | None = None
    t_deploy: float | None = None
    # Wall-clock seconds at resolve, which `host_wait` counts updater fires after.
    merged_at: float = 0.0
    merge_sha: str = ""
    verdict: str = ""
    cause: str = ""
    tags_label: str = ""
    lock_waited: int = 0
    lock_holder: str = ""
    kick: str = ""

    def __setattr__(self, name: str, value: object) -> None:
        """Coerce a `cause` outside `outcome.CAUSES` to `Cause.INVALID`; "" is no cause.

        Coerced and warned, not raised: this is written after the deploy ran, and a
        vocabulary typo must not turn a finished landing into a traceback with no
        `VERDICT:` line. The board then shows one `invalid-cause` bar to chase.
        """
        if name == "cause" and value and value not in CAUSES:
            print(
                f"land: unknown ledger cause {value!r}, recording as invalid-cause",
                file=sys.stderr,
            )
            value = Cause.INVALID
        super().__setattr__(name, value)


def _phase(a: float | None, b: float | None) -> str:
    return "" if a is None or b is None else str(int(b - a))


def annotation_line(ledger: Ledger, rc: int, total: float) -> str:
    """The logfmt record; an unreached stamp leaves its field empty, as land.sh did.

    `lock` counts the waits a wrapper rode out inside its own flock as well as the attempts
    that lost it, so it is non-zero on a landing whose every attempt exited 0.

    `host_lib.logfmt` quotes it (#6161). `holder` is quoted even when empty, because the
    line has always written it that way.
    """
    return logfmt(
        {
            "event": "landing",
            "pr": ledger.pr or "unknown",
            "sha": ledger.merge_sha[:8],
            "verdict": ledger.verdict or "aborted",
            "cause": ledger.cause,
            "exit": rc,
            "wait_merge": _phase(ledger.t_start, ledger.t_merged),
            "wait_ci": _phase(ledger.t_merged, ledger.t_ci),
            "tick": _phase(ledger.t_ci, ledger.t_tick),
            "deploy": _phase(ledger.t_tick, ledger.t_deploy),
            "total": int(total),
            "tags": ledger.tags_label or "none",
            "lock": ledger.lock_waited,
            "holder": ledger.lock_holder,
            "kick": ledger.kick,
        },
        always_quote=("holder",),
    )
