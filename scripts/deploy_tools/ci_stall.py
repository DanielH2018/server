"""The two shapes of a pending head whose CI never moves on its own, and the phrases naming them.

`await_ci.wait` appends `note`'s tail to its pending answer, and `land_lib/stall.py:of` reads
the phrase back out of that answer. A draft-skipped head got only a draft run, or a ready run the
draft run cancelled. On a no-run-left head every check-run finished and none decided it.
"""

from collections.abc import Callable

from lib import host_modules  # noqa: F401

from ci_verdict import (
    CI_GATE_NAME,
    CheckRun,
    CheckSuite,
    RunState,
    gates_failed_by_cancellation,
)

# Only a draft PR's run skips the `prek` gate, and a re-run replays the draft event.
DRAFT_SKIPPED = (
    "every CI run on it skipped the gate because the PR was a draft, or was cancelled"
)
NO_RUN_LEFT = "every CI run on it finished with no verdict, and none is running"


def draft_skipped(
    runs: list[CheckRun], required, read_suites: Callable[[], list[CheckSuite]]
) -> bool:
    """True when a gate run skipped and every other one failed only because it was cancelled.

    A draft run is the only shape that skips the gate, so no CI ran on such a head and none
    arrives until the PR is marked ready or pushed. A ready run that the draft run cancelled
    through ci.yml's concurrency group still runs its `always()` gate, which fails on the
    cancelled jobs. That gate carries no verdict either (`gates_failed_by_cancellation`). A
    `read_suites` call that fails reads as False.

    It reads the gate's own name rather than the jobs `jobs_verdict` aggregates: a skipped
    matrix job reports its unexpanded name, such as `pytest (shard ${{ matrix.shard }})`. A
    ready run's gate beside the skipped one makes this False, but the gate gets its check run
    only once every job it needs finishes, so a ready run still in progress reads as True;
    `land_lib/stall.py` reads the workflow runs before it acts on this.
    """
    seen = [r for r in runs if r.name in required]
    if not any(r.state is RunState.SKIPPED for r in seen):
        return False
    try:
        cancelled = gates_failed_by_cancellation(runs, read_suites)
    except OSError, ValueError:
        return False
    return all(
        r.state is RunState.SKIPPED
        or (r.name == CI_GATE_NAME and r.suite_id in cancelled)
        for r in seen
    )


def no_run_left(
    runs: list[CheckRun], required, read_suites: Callable[[], list[CheckSuite]]
) -> bool:
    """True when every check-run on a SHA whose verdict reads pending has completed.

    Nothing then moves the verdict: a cancelled run is never re-run on its own. Call it only
    after the verdict read `pending` and `draft_skipped` read False. A SHA with no required
    run counts only once an Actions suite for it ended without one, so a fresh push still
    reads False. A `read_suites` call that fails reads as False.
    """
    if any(r.status != "completed" for r in runs):
        return False
    if any(r.name in required for r in runs):
        return True
    try:
        return any(s.ended_without_a_run for s in read_suites())
    except OSError, ValueError:
        return False


def note(
    runs: list[CheckRun], required, read_suites: Callable[[], list[CheckSuite]]
) -> str:
    """The pending answer's tail naming a head no CI moves on its own, with its remedy; or ""."""
    if draft_skipped(runs, required, read_suites):
        return (
            f" — {DRAFT_SKIPPED}, and a re-run skips again; mark the PR ready, or, if it is "
            "ready already, convert it to draft and back, or push"
        )
    if no_run_left(runs, required, read_suites):
        return (
            f" — {NO_RUN_LEFT}; re-run its newest cancelled CI run with "
            "`gh run rerun <run-id>`, or push"
        )
    return ""
