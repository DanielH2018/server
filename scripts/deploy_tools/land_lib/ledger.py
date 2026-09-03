"""What the Landings board reads: one logfmt line per landing, from a Ledger of stamps.

This is the only record of where a landing's time goes: how long the merge took to arrive,
how long master CI took after it, the tick, the deploy. `lock` is seconds spent in tick or
deploy attempts that ended in lock contention, backoff included -- part of `tick` and
`deploy`, not a fifth phase.
"""

from __future__ import annotations

from dataclasses import dataclass


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
    tags_label: str = ""
    lock_waited: int = 0
    lock_holder: str = ""


def _phase(a: float | None, b: float | None) -> str:
    return "" if a is None or b is None else str(int(b - a))


def annotation_line(ledger: Ledger, rc: int, total: float) -> str:
    """The logfmt record; an unreached stamp leaves its field empty, as land.sh did."""
    return (
        f"event=landing pr={ledger.pr or 'unknown'} sha={ledger.merge_sha[:8]} "
        f"verdict={ledger.verdict or 'aborted'} exit={rc} "
        f"wait_merge={_phase(ledger.t_start, ledger.t_merged)} "
        f"wait_ci={_phase(ledger.t_merged, ledger.t_ci)} "
        f"tick={_phase(ledger.t_ci, ledger.t_tick)} "
        f"deploy={_phase(ledger.t_tick, ledger.t_deploy)} "
        f"total={int(total)} tags={ledger.tags_label or 'none'} "
        f'lock={ledger.lock_waited} holder="{ledger.lock_holder}"'
    )
