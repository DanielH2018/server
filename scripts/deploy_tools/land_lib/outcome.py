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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # scripts/
from lib.exit_codes import (
    DEPLOY_BAD_FLAGS,
    DEPLOY_BROAD,
    DEPLOY_LOCK_PLAN_FAILED,
    DEPLOY_LOCK_UNAVAILABLE,
    DEPLOY_NO_HOSTS,
    DEPLOY_STALE,
    FAILED,
    LAND_GAVE_UP,
)


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
    # Every stale retry lost the same race: master merged faster than one
    # tick-and-deploy cycle. Nothing was deployed and re-running is safe, which is the
    # opposite of what `deploy-failed` reads as. See `deploy.deploy_phase`.
    TIP_OUTRAN_RETRIES = "tip-outran-retries"


VERDICTS = frozenset(Verdict)


class Cause(StrEnum):
    """Which deploy failure a `deploy-failed` verdict was, for the board's `by (cause)`.

    The verdict alone cannot tell "nothing was deployed" (a tag miss, a failed tick, a
    host-lookup crash) from "changes are live and a task failed after them".

    DEPLOY_EXIT_* NAME THE deploy.sh CODES NO PHASE HANDLES ITSELF. They are enumerated
    rather than formatted from the return code, because `f"deploy-exit-{rc}"` made the set
    unbounded and the board groups by this field. `DEPLOY_EXIT_OTHER` is the bucket for a
    code outside deploy.sh's own contract, which should not happen.
    """

    TICK_HELD = "tick-held"
    TICK_FAILED = "tick-failed"
    HOST_LOOKUP = "host-lookup"
    TAG_MISS = "tag-miss"
    PLAYBOOK_FAILED = "playbook-failed"
    DEPLOY_EXIT_CD_FAILED = "deploy-exit-1"
    DEPLOY_EXIT_BROAD = "deploy-exit-3"
    DEPLOY_EXIT_STALE = "deploy-exit-4"
    DEPLOY_EXIT_BAD_FLAGS = "deploy-exit-64"
    DEPLOY_EXIT_LOCK_UNAVAILABLE = "deploy-exit-76"
    DEPLOY_EXIT_NO_HOSTS = "deploy-exit-78"
    DEPLOY_EXIT_LOCK_PLAN_FAILED = "deploy-exit-79"
    DEPLOY_EXIT_OTHER = "deploy-exit-other"
    INVALID = "invalid-cause"


CAUSES = frozenset(Cause)

# The deploy.sh exits `deploy_outcome` does not give a verdict of its own, keyed by the
# wrapper's contract (`exit_codes.py`). `tools.run_deploy` passes only `--tags`, so of these
# 1 (the `cd` into the primary checkout failed), 4 (stale tree) and 76 (flock failed on the
# lock file itself) can arrive today; 3 needs `--changed` and 64 needs `--detach`, and both
# are kept so a call site that adds either flag gets its label without editing this table.
# 76 also has its own arm in `deploy.deploy_outcome`, so it reaches here only from a caller
# that runs the wrapper without going through a landing. Anything outside the contract
# buckets as `deploy-exit-other` rather than inventing a label the board would group on.
_DEPLOY_EXIT_CAUSES = {
    FAILED: Cause.DEPLOY_EXIT_CD_FAILED,
    DEPLOY_BROAD: Cause.DEPLOY_EXIT_BROAD,
    DEPLOY_STALE: Cause.DEPLOY_EXIT_STALE,
    DEPLOY_BAD_FLAGS: Cause.DEPLOY_EXIT_BAD_FLAGS,
    DEPLOY_LOCK_UNAVAILABLE: Cause.DEPLOY_EXIT_LOCK_UNAVAILABLE,
    DEPLOY_NO_HOSTS: Cause.DEPLOY_EXIT_NO_HOSTS,
    DEPLOY_LOCK_PLAN_FAILED: Cause.DEPLOY_EXIT_LOCK_PLAN_FAILED,
}


def cause_for_deploy_exit(rc: int) -> Cause:
    """The bounded `cause` for a deploy.sh exit no phase names itself."""
    return _DEPLOY_EXIT_CAUSES.get(rc, Cause.DEPLOY_EXIT_OTHER)


class Outcome(Exception):
    """How a landing ends: the exit code, the verdict, and what to print.

    An exit-75 outcome must name a verdict, or the Landings board buckets it as
    `aborted` and it reads as lock contention.

    `error` is the `land: <error>` stderr line, and "" means there is none: `Landing.finish`
    raises without one. It is a `str` rather than `str | None` so a caller that reads it
    needs no narrowing.
    """

    def __init__(
        self, rc: int, detail: str, verdict: str | None = None, error: str = ""
    ) -> None:
        if verdict is not None and verdict not in VERDICTS:
            raise ValueError(f"unknown verdict {verdict!r}")
        if rc == LAND_GAVE_UP and verdict is None:
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

    def file_line(self) -> str:
        """The one line `--verdict-file` records: the VERDICT line, or why it stopped without one."""
        if self.verdict:
            return f"VERDICT: {self.verdict} ({self.detail})"
        return f"STOPPED: rc={self.rc} {self.detail}"


def write_verdict_file(path: str, line: str) -> None:
    """Replace `path` with `line`, whole: a reader sees the old line or the new one, never half.

    A lander unit hands this file to the agent that started it, instead of the landing's whole
    output, as renovate-agent's lander does with its own verdict file.
    """
    target = Path(path)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(line + "\n")
    tmp.replace(target)


def say(text: str) -> None:
    """A two-space-indented progress line, the shape the skill quotes."""
    print(f"  {text}")


# What a `behind` read means when this landing stopped watching a tick that was still
# applying. The ordinary `behind` prose says "the next tick crosses it", which
# is FALSE of a deployer parked on a failed apply -- no later tick crosses a hold -- and that
# is exactly the state the abandoned watch cannot rule out, because `hold_sha` is written
# after the apply returns. One string, printed by both verdict sites, so the two cannot drift.
ABANDONED_WATCH_NOTE = (
    "  This run stopped watching a tick that was still applying, so the markers above were "
    "read mid-apply: an apply that fails writes hold_sha only when it returns.\n"
    "  This is NOT the ordinary deferral. A later tick does not clear a hold, and a hold "
    "blocks every session's deploy. Re-run land.sh once the tick has settled."
)


def unrecorded_apply_note(behind_since: str | None) -> str:
    """What "no broad apply covers this PR" means, given whether the tick is still behind.

    Two states reach that branch, and only one of them is stranded. With `behind_since`
    empty the tick is level with origin and `next_action()` answers `noop` for every later
    tick, so nothing will ever apply this range. With it set the tick
    fast-forwarded only as far as a green ancestor of the tip — it crossed this PR's merge
    commit, which is why the landing is not BEHIND — and it has a range left to cross, so a
    later tick can still record the apply. One string, printed by both verdict sites, so the
    two cannot drift.

    Args:
      behind_since: the deployer's `behind_since` marker, as `Landing.state` returns it.
    """
    if behind_since:
        return (
            "  The tick fast-forwarded only as far as a green ancestor of the tip and is "
            "still behind origin, so a later tick may yet apply this range. Apply it by hand "
            "if you would rather not wait."
        )
    return (
        "  Something OTHER than the tick fast-forwarded the checkout, so the tick will "
        "never see this range again."
    )


def remaining_hosts_note(remaining: str) -> str:
    """The remaining-hosts remediation, printed BESIDE a plane the same PR owes a hand.

    Both `needs-manual-apply` sites end at their plane arm before reaching the branch that
    owns the remaining-hosts verdict, so a PR carrying both halves printed only the plane's
    command.

    It claims nothing about the tick, unlike the wording the remaining-hosts verdict itself
    prints: the plane arm ends the landing before the tick's own state is read, so whether
    the tick applied the role on this host is unknown here. The hosts below are owed the
    role either way. One string, printed by both verdict sites, so the two cannot drift.
    """
    return f"  STILL UNAPPLIED on the hosts this tick never touched: {remaining}"
