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
    # GitHub never ran CI for the merge commit, and no later master commit stands in for it:
    # none has run CI either, or the nearest is red with a green one after it. No re-run
    # converges on its own, so this is not a resume point. The detail names the tip and the
    # way out. See `await_ci.wait` (#4641, #4649).
    CI_UNPROVEN = "ci-unproven"
    # The landing stopped on a refusal or an error of its own before it reached any other
    # verdict: the landing policy, a PR closed unmerged or with changes requested, a read
    # that failed. `Outcome` gives every exit-1 end this verdict when its site named none,
    # so no exit-1 landing ends without a `VERDICT:` line (#4451). The detail says which.
    REFUSED = "refused"
    # land.sh handed the landing to a lander unit (`handoff.py`) and stopped waiting on it
    # before it ended. The unit may still be running, and a re-run waits on it again.
    HANDOFF_TIMEOUT = "handoff-timeout"
    # A host the landing would wait on has no package at or after the merge, and the builder
    # reports a failing reason, so no updater fire can apply it. A re-run converges once the
    # builder publishes. See `host_published.unbuilt_hosts` (#7024).
    UNBUILT = "unbuilt"


VERDICTS = frozenset(Verdict)


class Cause(StrEnum):
    """Which deploy failure a `deploy-failed` verdict was, for the board's `by (cause)`.

    The verdict alone cannot tell a tag nothing applies from a held promotion or a failed soak.

    PROMOTE_* NAME HOW A WAIT ON PROMOTION ENDED (`promote.py land-wait`), for a PR whose tag
    Flux applies. `promote-held` is DEPLOY_HOLD holding, beside `deploy-failed`.
    `soak-failed` is R's `flux/soak` reading `failure`, beside `deploy-failed`, read before
    `verify` writes the hold that would end the wait as `promote-held` a poll later (D5.3b3).
    `promote-timeout` is the wait reaching its deadline: twice the promote timeout, counted
    again from each poll that reads an earlier commit's verification in flight. It rides beside
    `deferred`: the promotion applies this PR itself, and a later one may still cover it.

    GATE_* NAME A HEALTH GATE THAT GRADED NOTHING, beside `deferred` from step 6 (#7911).
    `gate-unreadable` is a gate that could not read its store, and `gate-stale` a gate whose
    tree was older than `origin/master`. Nothing gates health again after either, so
    `land_probe` reads them as a give-up rather than a plain deferral.
    """

    TAG_MISS = "tag-miss"
    PROMOTE_HELD = "promote-held"
    PROMOTE_TIMEOUT = "promote-timeout"
    GATE_UNREADABLE = "gate-unreadable"
    GATE_STALE = "gate-stale"
    SOAK_FAILED = "soak-failed"
    INVALID = "invalid-cause"


CAUSES = frozenset(Cause)


class Outcome(Exception):
    """How a landing ends: the exit code, the verdict, and what to print.

    An exit-75 outcome must name a verdict, or the Landings board buckets it as
    `aborted` and it reads as lock contention. An exit-1 outcome that names none takes
    `refused`, so every exit-1 end prints a `VERDICT:` line too (#4451). Exit 64, a usage
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


# What a `deferred` from step 6 means, printed by `health_verdict.health` when `probe.py health`
# could not read its store for a tag and found no other tag unhealthy (#5543). Nothing re-gates
# health, so this deferral never ends on its own. The landing records `Cause.GATE_UNREADABLE`,
# and `land_probe` reports it as `gave-up`, a resume point, as it does for `promote-timeout`.
UNREADABLE_GATE_NOTE = (
    "  The health gate could not read its store, so the deployed workloads were not graded. "
    "This is NOT the ordinary deferral: nothing gates health again.\n"
    "  Re-run land.sh to gate again once the store answers; Prometheus rolling is the usual "
    "cause."
)


# What a `deferred` from step 6 means when the probe refused the gate's tree as older than
# `origin/master` (#7816). The gate advances its tree to `origin/master`, so this is a master
# that moved while the gate ran, and a re-run reads the newer tip. The landing records
# `Cause.GATE_STALE`, and `land_probe` reports it as `gave-up`, a resume point.
STALE_GATE_NOTE = (
    "  The health gate's tree held an older role definition than origin/master, so those "
    "workloads were not graded. This is NOT the ordinary deferral: nothing gates health again.\n"
    "  Re-run land.sh; it gates from the origin/master it reads then. No store failed to answer."
)


def disarmed(where: str, consequence: str) -> str:
    """The message for await_ci's CI_DISARMED, shared by ci.py's step 3 and merge.py's direct merge.

    Not a CI result: await_ci refused to report one because its required-context set is
    empty. Waiting or re-running changes nothing; re-arming the gate does (#4464).
    """
    return (
        f"the CI gate is disarmed{where} — await_ci requires no CI contexts, so it "
        f"reports no verdict; {consequence}. To re-arm it, set REQUIRED_CONTEXTS in "
        "scripts/deploy_tools/await_ci.py to ci_required_contexts in inventory/group_vars/all.yml"
    )


def host_not_waited_note(targets: list) -> str:
    """The line naming each host target a landing ended without waiting on."""
    hosts = ", ".join(t.host for t in targets)
    return f"  it also changes the package of {hosts}; this landing did not wait for it to apply"
