"""The phase order, and nothing else.

Each phase is a function in its own module; this is the only place that knows the
sequence. `run` turns the Outcome a phase raises into a return value, so main() has one
thing to print and one thing to annotate.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib import ci, classify, deploy, merge, tick
from deploy_tools.land_lib.landing import Landing
from deploy_tools.land_lib.outcome import Outcome


def run(ln: Landing) -> Outcome:
    """Run every phase in order; the Outcome that ended it."""
    try:
        _phases(ln)
    except Outcome as outcome:
        ln.ledger.verdict = outcome.verdict or ""
        return outcome
    raise AssertionError("the last phase must end the landing")


def _phases(ln: Landing) -> None:
    if ln.opts.arm_merge:
        merge.arm_merge(ln)
    if ln.opts.await_merge:
        merge.await_merge(ln)
    classify.resolve(ln)
    classify.classify(ln)
    classify.shortcut_if_nothing(ln)
    ci.preflight(ln)
    print("== 3/6  waiting for master CI")
    ci.wait_master_ci(ln, ln.merge_sha, ln.merge_sha)
    ln.ledger.t_ci = ln.tools.clock()
    print("== 4/6  GitOps tick (fetch, ff-merge, deploy what is eligible)")
    tick.run_tick(ln)
    ln.ledger.t_tick = ln.tools.clock()
    deploy.deploy_phase(ln)
    ln.die("health phase not yet ported", 1)
