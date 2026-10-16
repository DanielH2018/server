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
    LAND_FAILED,
    LAND_GAVE_UP,
)
from lib.repo_paths import GITOPS_DEPLOY_FILES

sys.path.insert(0, str(GITOPS_DEPLOY_FILES))
from gitops_markers import HOLD_CLEAR_CMD


def hold_clear_hint(sha: str | None) -> str:
    """How a hand clears the deployer's hold on `sha` once every held plane is applied.

    `gitops_state.py clear-hold` takes the FULL held SHA and refuses any other, so `sha` is
    never sliced here (#4015, #4081).
    """
    return (
        "Once every held plane is applied, clear the hold with Clear in the deploy UI "
        f"or `{HOLD_CLEAR_CMD} {sha}` (CLAUDE.md, When to wait)"
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
    # GitHub never ran CI for the merge commit, and no later master commit stands in for it:
    # none has run CI either, or the nearest is red with a green one after it. No re-run
    # converges on its own, so this is not a resume point. The detail names the tip and the
    # way out. See `await_ci.wait` (#4641, #4649).
    CI_UNPROVEN = "ci-unproven"
    LOCK_BUSY = "lock-busy"
    # Every stale retry lost the same race: master merged faster than one
    # tick-and-deploy cycle. Nothing was deployed and re-running is safe, which is the
    # opposite of what `deploy-failed` reads as. See `deploy.deploy_phase`.
    TIP_OUTRAN_RETRIES = "tip-outran-retries"
    # The landing stopped on a refusal or an error of its own before it reached any other
    # verdict: the landing policy, a PR closed unmerged or with changes requested, a read
    # that failed. `Outcome` gives every exit-1 end this verdict when its site named none,
    # so no exit-1 landing ends without a `VERDICT:` line (#4451). The detail says which.
    REFUSED = "refused"
    # land.sh handed the landing to a lander unit (`handoff.py`) and stopped waiting on it
    # before it ended. The unit may still be running, and a re-run waits on it again.
    HANDOFF_TIMEOUT = "handoff-timeout"


VERDICTS = frozenset(Verdict)


class Cause(StrEnum):
    """Which deploy failure a `deploy-failed` verdict was, for the board's `by (cause)`.

    The verdict alone cannot tell "nothing was deployed" (a tag miss, a failed tick, a
    host-lookup crash) from "changes are live and a task failed after them".

    DEPLOY_EXIT_* NAME THE deploy.sh CODES NO PHASE HANDLES ITSELF. They are enumerated
    rather than formatted from the return code, because `f"deploy-exit-{rc}"` made the set
    unbounded and the board groups by this field. `DEPLOY_EXIT_OTHER` is the bucket for a
    code outside deploy.sh's own contract, which should not happen.

    PROMOTE_* NAME HOW A WAIT ON PROMOTION ENDED (`promote.py land-wait`), for a PR whose tag
    Flux applies. `promote-held` is DEPLOY_HOLD holding, beside `deploy-failed`.
    `promote-timeout` is the wait reaching its deadline: twice the promote timeout, counted
    again from each poll that reads an earlier commit's verification in flight. It rides beside
    `deferred`, as `deploy-exit-4` rides beside `tip-outran-retries`: the promotion applies
    this PR itself, and a later one may still cover it.
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
    PROMOTE_HELD = "promote-held"
    PROMOTE_TIMEOUT = "promote-timeout"
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
    `aborted` and it reads as lock contention. An exit-1 outcome that names none takes
    `refused`, so every exit-1 end prints a `VERDICT:` line too (#4451). Exit 64, a usage
    error, prints none.

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
        if rc == LAND_FAILED and verdict is None:
            verdict = Verdict.REFUSED
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


# What a `deferred` with cause `promote-timeout` means, printed by `promotion.wait_on_promotion`.
# The ordinary deferral is a tick that will cross the PR on its own. This one may not end on its
# own: a publish refusal leaves no `sha-<commit>` tag to promote. `land_probe` reads the note and
# reports the landing as `gave-up`, a resume point, as it does for ABANDONED_WATCH_NOTE.
PROMOTE_TIMEOUT_NOTE = (
    "  No promotion covered this merge commit in time. This is NOT the ordinary deferral: a "
    "publish refusal leaves no sha-<commit> tag to promote.\n"
    "  Read the `promote` workflow's failed `publish` jobs first, then re-run land.sh."
)


def unrecorded_apply_note(behind_since: str | None) -> str:
    """What "no broad apply covers this PR" means, given whether the tick is still behind.

    A third state reaches that branch first: the tick refused to narrow the deploy plane and
    deferred it to `k8s_unapplied` (#4333). `deferred_plane_note` owns that one, and
    `Landing.tick_half_remediation` decides between the two.

    Two states reach this note, and only one of them is stranded. With `behind_since`
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


def deferred_plane_note(owed: int | None) -> str:
    """What "no broad apply covers this PR" means when the tick deferred the deploy plane.

    The tick fast-forwarded this PR itself, could not narrow its deploy plane, and recorded
    every service the plane may reach as a `k8s_unapplied` line instead of running the whole
    `ansible/deploy.yml` (#4333). Each line discharges on a deploy of its service or on the
    render proof (#4841), so the stranded-range remedy is wrong twice over: it runs the
    twenty-minute play the tick deliberately skipped, and its ff-merge on the primary checkout
    cancels the tick's deploys (#4843). One string, printed by every site that reaches this
    state, so they cannot drift.

    Args:
      owed: how many of the deferred services still hold a ledger line, or None when the
        ledger could not be read.
    """
    count = "an unknown number" if owed is None else str(owed)
    return (
        "  The tick fast-forwarded this PR but could not narrow its deploy plane, so it "
        f"deferred the plane to the k8s_unapplied ledger: {count} of its services are still "
        "owed, instead of a whole ansible/deploy.yml run.\n"
        "  Each discharges once a `./scripts/deploy.sh --tags <service>` deploy or the "
        "render proof covers it, and the plane is applied when none is left. List them with "
        "`uv run python scripts/diagnostics/probe.py gitops-state`."
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


def disarmed(where: str, consequence: str) -> str:
    """The message for await_ci's CI_DISARMED, shared by ci.py's step 3 and merge.py's direct merge.

    Not a CI result: await_ci refused to report one because its required-context set is
    empty. Waiting or re-running changes nothing; re-arming the gate does (#4464).
    """
    return (
        f"the CI gate is disarmed{where} — await_ci requires no CI contexts, so it "
        f"reports no verdict; {consequence}. To re-arm it, set REQUIRED_CONTEXTS in "
        "scripts/deploy_tools/await_ci.py to the deployer's gitops_deploy_ci_contexts"
    )
