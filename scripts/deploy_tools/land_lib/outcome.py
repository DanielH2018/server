"""The words a landing ends with: the verdict set, the cause set, the Outcome, and say().

An Outcome is raised by `Landing.die` and `Landing.finish` and returned by `pipeline.run`.
It carries verdict and exit code together, so neither can be printed without the other --
land.sh assigned LAND_VERDICT on the line before each `exit`, and a test grepped that they
were adjacent.

BOTH VOCABULARIES ARE CLOSED, AND BOTH ARE READ BY THE LANDINGS BOARD. `Verdict` is what the
`VERDICT:` line prints; `Cause` is the one-token reason beside a `deploy-failed` verdict in
the logfmt annotation, and the board groups by it. They are `StrEnum`s so `ty` catches a typo
at the assignment rather than when the branch runs; the string values are unchanged, and the
runtime check in `Outcome.__init__` stays for a value that arrives from outside.
"""

import sys
from enum import StrEnum


class Verdict(StrEnum):
    """How a landing ended, as printed on the `VERDICT:` line."""

    SETTLED = "settled"
    UNHEALTHY = "unhealthy"
    DEPLOY_FAILED = "deploy-failed"
    NOTHING_TO_DEPLOY = "nothing-to-deploy"
    BLOCKED = "blocked"
    NEEDS_MANUAL_APPLY = "needs-manual-apply"
    DEFERRED = "deferred"
    MERGE_CONFLICT = "merge-conflict"
    PR_CI_RED = "pr-ci-red"
    MERGE_TIMEOUT = "merge-timeout"
    CI_RED = "ci-red"
    CI_TIMEOUT = "ci-timeout"
    LOCK_BUSY = "lock-busy"


VERDICTS = frozenset(Verdict)


class Cause(StrEnum):
    """Which deploy failure a `deploy-failed` verdict was, for the board's `by (cause)`.

    The verdict alone cannot tell "nothing was deployed" (a tag miss, a failed tick, a
    host-lookup crash) from "changes are live and a task failed after them".

    DEPLOY_EXIT_* NAME THE deploy.sh CODES NO PHASE HANDLES ITSELF. They are enumerated
    rather than formatted from the return code, because `f"deploy-exit-{rc}"` made the set
    unbounded and the board groups by this field. `DEPLOY_EXIT_OTHER` is the bucket for a
    code outside deploy.sh's own contract, which should not happen and previously would have
    become its own label.
    """

    TICK_HELD = "tick-held"
    TICK_FAILED = "tick-failed"
    HOST_LOOKUP = "host-lookup"
    TAG_MISS = "tag-miss"
    PLAYBOOK_FAILED = "playbook-failed"
    DEPLOY_EXIT_BROAD = "deploy-exit-3"
    DEPLOY_EXIT_STALE = "deploy-exit-4"
    DEPLOY_EXIT_BAD_FLAGS = "deploy-exit-64"
    DEPLOY_EXIT_OTHER = "deploy-exit-other"


CAUSES = frozenset(Cause)

# The deploy.sh exits `deploy_outcome` does not give a verdict of its own. Every value here
# was already emitted verbatim as `deploy-exit-<rc>`, so the board's existing labels are
# unchanged; anything outside this table now buckets rather than inventing a label.
_DEPLOY_EXIT_CAUSES = {
    3: Cause.DEPLOY_EXIT_BROAD,
    4: Cause.DEPLOY_EXIT_STALE,
    64: Cause.DEPLOY_EXIT_BAD_FLAGS,
}


def cause_for_deploy_exit(rc: int) -> Cause:
    """The bounded `cause` for a deploy.sh exit no phase names itself."""
    return _DEPLOY_EXIT_CAUSES.get(rc, Cause.DEPLOY_EXIT_OTHER)


class Outcome(Exception):
    """How a landing ends: the exit code, the verdict, and what to print.

    An exit-75 outcome must name a verdict: before 2026-09-02 four of them reached the
    Landings board as one `aborted` bucket and were taken for lock contention.
    """

    def __init__(
        self, rc: int, detail: str, verdict: str | None = None, error: str | None = None
    ) -> None:
        if verdict is not None and verdict not in VERDICTS:
            raise ValueError(f"unknown verdict {verdict!r}")
        if rc == 75 and verdict is None:
            raise ValueError("an exit-75 outcome must name a verdict")
        super().__init__(detail)
        self.rc = rc
        self.detail = detail
        self.verdict = verdict
        self.error = error

    def emit(self) -> None:
        """Print `land: <error>` (stderr) and `VERDICT: <verdict> (<detail>)` (stdout), as present."""
        if self.error:
            print(f"land: {self.error}", file=sys.stderr)
        if self.verdict:
            print(f"VERDICT: {self.verdict} ({self.detail})")


def say(text: str) -> None:
    """A two-space-indented progress line, the shape the skill quotes."""
    print(f"  {text}")
