"""What the Landings board reads: one logfmt line per landing, from a Ledger of stamps.

This is the only record of where a landing's time goes: how long the merge took to arrive,
how long master CI took after it, the tick, the deploy. `lock` is seconds spent in tick or
deploy attempts that ended in lock contention, backoff included -- part of `tick` and
`deploy`, not a fifth phase.

`cause` is a one-token reason beside a `deploy-failed` verdict, and empty beside every other
one. The verdict alone cannot tell "nothing was deployed" (a tag miss, a failed tick, a
host-lookup crash) from "changes are live and a task failed after them", and the board had
no way to split them (issue #1031). Its vocabulary is `outcome.Cause` and it is checked HERE,
on assignment, because the board groups by the field: a value invented at one of the seven
writing sites becomes a bar on the dashboard, and nothing downstream would reject it.
"""

from dataclasses import dataclass

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib.outcome import CAUSES


@dataclass
class Ledger:
    """The stamps and labels the annotation is built from. Phases write; annotation reads."""

    pr: str
    t_start: float
    t_merged: float | None = None
    t_ci: float | None = None
    t_tick: float | None = None
    t_deploy: float | None = None
    merge_sha: str = ""
    verdict: str = ""
    cause: str = ""
    tags_label: str = ""
    lock_waited: int = 0
    lock_holder: str = ""

    def __setattr__(self, name: str, value: object) -> None:
        """Reject a `cause` outside `outcome.CAUSES`; "" stays the no-cause value."""
        if name == "cause" and value and value not in CAUSES:
            raise ValueError(f"unknown ledger cause {value!r}")
        super().__setattr__(name, value)


def _phase(a: float | None, b: float | None) -> str:
    return "" if a is None or b is None else str(int(b - a))


def annotation_line(ledger: Ledger, rc: int, total: float) -> str:
    """The logfmt record; an unreached stamp leaves its field empty, as land.sh did."""
    return (
        f"event=landing pr={ledger.pr or 'unknown'} sha={ledger.merge_sha[:8]} "
        f"verdict={ledger.verdict or 'aborted'} cause={ledger.cause} exit={rc} "
        f"wait_merge={_phase(ledger.t_start, ledger.t_merged)} "
        f"wait_ci={_phase(ledger.t_merged, ledger.t_ci)} "
        f"tick={_phase(ledger.t_ci, ledger.t_tick)} "
        f"deploy={_phase(ledger.t_tick, ledger.t_deploy)} "
        f"total={int(total)} tags={ledger.tags_label or 'none'} "
        f'lock={ledger.lock_waited} holder="{ledger.lock_holder}"'
    )
