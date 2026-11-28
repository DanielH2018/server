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

from lib.exit_codes import LAND_FAILED, LAND_GAVE_UP


class Verdict(StrEnum):
    """How a landing ended, as printed on the `VERDICT:` line."""

    SETTLED = "settled"
    UNHEALTHY = "unhealthy"
    DEPLOY_FAILED = "deploy-failed"
    NOTHING_TO_DEPLOY = "nothing-to-deploy"
    NEEDS_MANUAL_APPLY = "needs-manual-apply"
    DEFERRED = "deferred"
    MERGE_CONFLICT = "merge-conflict"
    PR_CI_RED = "pr-ci-red"
    MERGE_TIMEOUT = "merge-timeout"
    CI_RED = "ci-red"
    CI_TIMEOUT = "ci-timeout"
    # GitHub never ran CI for the merge commit and no later master commit stands in for it. No
    # re-run converges, so this is not a resume point.
    CI_UNPROVEN = "ci-unproven"
    # The landing stopped on a refusal or an error of its own before it reached any other
    # verdict: the landing policy, a PR closed unmerged or with changes requested, a read
    # that failed. `Outcome` gives every exit-1 end this verdict when its site named none,
    # so no exit-1 landing ends without a `VERDICT:` line. The detail says which.
    REFUSED = "refused"
    HANDOFF_TIMEOUT = "handoff-timeout"
    # A host the landing waits on has no package at or after the merge and the builder reports a
    # failing reason. A re-run converges once the builder publishes.
    UNBUILT = "unbuilt"


VERDICTS = frozenset(Verdict)


class Cause(StrEnum):
    """Which end of its verdict a landing reached, for the board's `by (cause)` and `NEXT`.

    The verdict alone cannot tell a tag nothing applies from a held promotion or a failed soak,
    and each of those needs a different next action.

    PROMOTE_* NAME HOW A WAIT ON PROMOTION ENDED (`promote.py land-wait`), for a PR whose tag
    Flux applies. `promote-held` is DEPLOY_HOLD holding, beside `deploy-failed`.
    `soak-failed` is R's `flux/soak` reading `failure`, beside `deploy-failed`, read before
    `verify` writes the hold that would end the wait as `promote-held` a poll later (D5.3b3).
    `promote-timeout` is the wait reaching its deadline: twice the promote timeout, counted
    again from each poll that reads an earlier commit's verification in flight. It rides beside
    `deferred`: the promotion applies this PR itself, and a later one may still cover it.

    GATE_* NAME A HEALTH GATE THAT GRADED NOTHING, beside `deferred` from step 6.
    `gate-unreadable` is a gate that could not read its store, and `gate-stale` a gate whose
    tree was older than `origin/master`. Nothing gates health again after either, so
    `land_probe` reads them as a give-up rather than a plain deferral. A promotion wait that
    found no kubectl or no readable kubeconfig ends `gate-unreadable` too: nothing it waits on
    can be read until the host's setup is fixed.

    `landing-live` rides beside a `refused` with exit 75 that started no landing, because
    another landing of the PR holds its lock (`pr_lock.py`). It is on the `LAND-RESULT:` line
    only: that stop writes no annotation, so the board never counts it.

    `updater-not-due` rides beside `deferred` from the host wait, when a host's updater timer
    is not armed or its next fire is past the wait's 45 minutes, so the detail names the
    updater's start command. Starting it needs root.

    `own-red` rides beside `ci-red` when master CI is red on the merge commit itself and green
    on its first parent: this PR's change turned master red, and no wait clears it.

    `master-moved` rides beside a `refused` with exit 1 from a landing a fresh checkout ran
    (`fresh.CHILD_ENV`), when master moved past its landing code again before the merge.
    Nothing merged, and the same command re-runs from a newer checkout.
    """

    TAG_MISS = "tag-miss"
    PROMOTE_HELD = "promote-held"
    PROMOTE_TIMEOUT = "promote-timeout"
    GATE_UNREADABLE = "gate-unreadable"
    GATE_STALE = "gate-stale"
    SOAK_FAILED = "soak-failed"
    LANDING_LIVE = "landing-live"
    UPDATER_NOT_DUE = "updater-not-due"
    OWN_RED = "own-red"
    MASTER_MOVED = "master-moved"
    INVALID = "invalid-cause"


CAUSES = frozenset(Cause)


class Next(StrEnum):
    """What the session that ran a landing does next, as `LAND-RESULT:`'s `next` field prints it.

    `wait:<source>` names a `cc-wait` source to wait on before re-running the same `land.sh`
    command. `follow:land` waits on another landing of the PR with `cc-wait land`, and that
    landing's result is this PR's: re-running `land.sh` would only be refused again.
    `dispatch-and-rerun` dispatches the workflow runs the detail names, then re-runs, and
    `fix-builder-and-rerun` repairs the host package builder, then re-runs. Neither changes
    the PR.
    """

    DONE = "done"
    RERUN = "rerun"
    WAIT_LANDING_CLEAR = "wait:landing-clear"
    FOLLOW_LAND = "follow:land"
    FIX_AND_RERUN = "fix-and-rerun"
    DISPATCH_AND_RERUN = "dispatch-and-rerun"
    FIX_BUILDER_AND_RERUN = "fix-builder-and-rerun"
    APPLY_BY_HAND = "apply-by-hand"
    OPERATOR = "operator"


# Each verdict's next action, keyed `(verdict, None)`, and the causes or exit codes that change
# it, keyed `(verdict, cause)` or `(verdict, exit code)`. The cause wins, then the exit code.
# Taken from `.claude/skills/land-after-merge/SKILL.md` (*Reading the verdict*) and
# `docs/landing.md`, which say what each verdict needs.
NEXT: dict[tuple[str, str | int | None], Next] = {
    (Verdict.SETTLED, None): Next.DONE,
    (Verdict.NOTHING_TO_DEPLOY, None): Next.DONE,
    # A host's updater applies its package; cc-wait ends it with exit 4.
    (Verdict.DEFERRED, None): Next.DONE,
    (Verdict.DEFERRED, Cause.PROMOTE_TIMEOUT): Next.FIX_AND_RERUN,
    (Verdict.DEFERRED, Cause.GATE_UNREADABLE): Next.RERUN,
    (Verdict.DEFERRED, Cause.GATE_STALE): Next.RERUN,
    (Verdict.DEFERRED, Cause.UPDATER_NOT_DUE): Next.OPERATOR,
    (Verdict.UNHEALTHY, None): Next.FIX_AND_RERUN,
    # Without a cause, a host's reconcile failed, and its `--clear-hold` needs sudo.
    (Verdict.DEPLOY_FAILED, None): Next.OPERATOR,
    (Verdict.DEPLOY_FAILED, Cause.TAG_MISS): Next.FIX_AND_RERUN,
    (Verdict.DEPLOY_FAILED, Cause.PROMOTE_HELD): Next.WAIT_LANDING_CLEAR,
    (Verdict.DEPLOY_FAILED, Cause.SOAK_FAILED): Next.OPERATOR,
    (Verdict.NEEDS_MANUAL_APPLY, None): Next.APPLY_BY_HAND,
    (Verdict.MERGE_CONFLICT, None): Next.FIX_AND_RERUN,
    (Verdict.PR_CI_RED, None): Next.FIX_AND_RERUN,
    (Verdict.MERGE_TIMEOUT, None): Next.RERUN,
    (Verdict.CI_RED, None): Next.WAIT_LANDING_CLEAR,
    (Verdict.CI_RED, Cause.OWN_RED): Next.FIX_AND_RERUN,
    (Verdict.CI_TIMEOUT, None): Next.RERUN,
    (Verdict.CI_UNPROVEN, None): Next.DISPATCH_AND_RERUN,
    (Verdict.REFUSED, None): Next.FIX_AND_RERUN,
    # A landing rule refused before the merge (`slots.py`), and a re-run waits in line again.
    (Verdict.REFUSED, LAND_GAVE_UP): Next.RERUN,
    (Verdict.REFUSED, Cause.LANDING_LIVE): Next.FOLLOW_LAND,
    (Verdict.REFUSED, Cause.MASTER_MOVED): Next.RERUN,
    (Verdict.HANDOFF_TIMEOUT, None): Next.RERUN,
    (Verdict.UNBUILT, None): Next.FIX_BUILDER_AND_RERUN,
}


def next_action(verdict: str | None, cause: str, rc: int) -> Next | None:
    """The `NEXT` row for a landing that ended `verdict`, beside `cause`, with exit `rc`.

    Returns None for a landing that named no verdict, such as a usage error, and `operator`
    for a verdict with no row: `result_line` runs just above the `VERDICT:` line, which a
    raise here would leave unprinted.
    """
    if verdict is None:
        return None
    for key in ((verdict, cause), (verdict, rc), (verdict, None)):
        if key in NEXT:
            return NEXT[key]
    return Next.OPERATOR


class Outcome(Exception):
    """How a landing ends: the exit code, the verdict, and what to print.

    An exit-75 outcome must name a verdict, or the Landings board buckets it as
    `aborted` and it reads as lock contention. An exit-1 outcome that names none takes
    `refused`, so every exit-1 end prints a `VERDICT:` line too. Exit 64, a usage
    error, prints none.

    `recorded` is the detail `--verdict-file` records when it differs from the printed one,
    such as a broad landing's full tag list where the VERDICT line prints a count.

    `error` is the `land: <error>` stderr line, printed only when no VERDICT line carries it.
    "" means there is none: `Landing.finish` raises without one. It is a `str` rather than
    `str | None` so a caller that reads it needs no narrowing.
    """

    def __init__(
        self,
        rc: int,
        detail: str,
        verdict: str | None = None,
        error: str = "",
        recorded: str = "",
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
        self.recorded = recorded or detail

    def emit(self) -> None:
        """Print `VERDICT: <verdict> (<detail>)` on stdout, else `land: <error>` on stderr.

        The VERDICT detail already carries the error, so a stop prints the sentence once.
        """
        if self.error and not self.verdict:
            print(f"land: {self.error}", file=sys.stderr)
        if self.verdict:
            print(f"VERDICT: {self.verdict} ({self.detail})")

    def add_notes(self, notes: list[str]) -> None:
        """Append `notes` to the detail, both the printed and the recorded one."""
        self.detail = "; ".join((self.detail, *notes))
        self.recorded = "; ".join((self.recorded, *notes))

    def file_line(self) -> str:
        """The one line `--verdict-file` records: the VERDICT line, or why it stopped without one."""
        if self.verdict:
            return f"VERDICT: {self.verdict} ({self.recorded})"
        return f"STOPPED: rc={self.rc} {self.recorded}"


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


# What a `deferred` with cause `promote-timeout` means, printed by `promotion.wait_on_promotion`.
# This deferral may not end on its own: a publish refusal leaves no `sha-<commit>` tag to
# promote. `land_probe` reads the note and reports the landing as `gave-up`, a resume point.
PROMOTE_TIMEOUT_NOTE = (
    "  No promotion covered this merge commit in time. This is NOT the ordinary deferral: a "
    "publish refusal leaves no sha-<commit> tag to promote.\n"
    "  Read the `promote` workflow's failed `publish` jobs first, then re-run land.sh."
)


# Nothing re-gates health, so this deferral never ends on its own. Recorded as
# `Cause.GATE_UNREADABLE`, reported by `land_probe` as `gave-up`, a resume point.
UNREADABLE_GATE_NOTE = (
    "  The health gate could not read its store, so the deployed workloads were not graded. "
    "This is NOT the ordinary deferral: nothing gates health again.\n"
    "  Re-run land.sh to gate again once the store answers; Prometheus rolling is the usual "
    "cause."
)


# The master moved while the gate ran, and a re-run reads the newer tip. Recorded as
# `Cause.GATE_STALE`, reported as `gave-up`, a resume point.
STALE_GATE_NOTE = (
    "  The health gate's tree held an older role definition than origin/master, so those "
    "workloads were not graded. This is NOT the ordinary deferral: nothing gates health again.\n"
    "  Re-run land.sh; it gates from the origin/master it reads then. No store failed to answer."
)


def disarmed(where: str, consequence: str) -> str:
    """The message for await_ci's CI_DISARMED, shared by ci.py's step 3 and merge.py's direct merge.

    Not a CI result: await_ci refused to report one because its required-context set is
    empty. Waiting or re-running changes nothing; re-arming the gate does.
    """
    return (
        f"the CI gate is disarmed{where} — await_ci requires no CI contexts, so it "
        f"reports no verdict; {consequence}. To re-arm it, set REQUIRED_CONTEXTS in "
        "scripts/deploy_tools/await_ci.py to ci_required_contexts in estate/vars.yml"
    )


def host_not_waited_note(targets: list) -> str:
    """The line naming each host target a landing ended without waiting on."""
    hosts = ", ".join(t.host for t in targets)
    return f"  it also changes the package of {hosts}; this landing did not wait for it to apply"
