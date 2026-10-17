"""`promote.py reset-check`: whether a `reset_to_last_good` dispatch moves `prod` back.

`promote.yml`'s `reset_to_last_good` input moves `prod` to `last-good` and does nothing else
(D5.2b2). It follows contract C2's split: this read-only job decides, and the `reset-move`
job, with no checkout and `packages: write`, compares and moves the tag. Both jobs run in the
concurrency group `promote-reset`, not `promote`. In `promote`, the reset would wait behind a
`decide` that waits on a verification in flight, for up to 6 hours, and the next merge's run
would evict it while it was pending. The reset ignores the reboot window, because it moves
`prod` back to a revision the cluster already ran, on the operator's dispatch.

THE CHECKS, in order. Each refusal exits 1 and names its reason.
  1. DEPLOY_HOLD, read by D1's rule. `none`, an empty value, a missing variable and a failed
     read all refuse. Without a hold the next promote run would move `prod` forward again.
  2. `prod`'s digest and `last-good`'s, by the same two HEADs `decide` polls. Either one
     absent or unreadable refuses.
  3. The two digests are equal: `prod` already names `last-good`. Nothing moves, and the
     check succeeds.
  4. The two digests differ. The reset proceeds only when DEPLOY_HOLD's first word is a
     40-hex SHA X and the manifest digest of `sha-<X>` equals `prod`'s. Such a hold means
     the run that moved `prod` to X has settled and left `prod` there: the stateful hold
     `<T> stateful=<names>`, H1's `<T>`, or C2's `<T> failed=<names>` and `<T> unverified`
     after `settle`'s own reset failed. The check compares the digests rather than trusting
     the SHA string. Any other difference means a run is between its `tag` and its `settle`,
     so the check refuses. The refusal names `prod`'s commit and the in-progress `promote`
     runs, and tells the operator to wait for that run's `settle` or to cancel it, then to
     read `last-good` again and dispatch again.

Which holds pass is not a policy on what may be reset. Each runbook rules on that
(docs/target-architecture/deploy/d5-bulk-move.md, *A reset input*).

OUTPUT. One line, `verdict=<move|none|refused> prod=<digest> last_good=<digest>`, with the
reason on stderr. `$GITHUB_OUTPUT` gets `move`, `true` only for check 4's pass, and `prod`
and `last_good`, the two digests `reset-move` compares and moves with.

Run: uv run python scripts/deploy_tools/promote.py reset-check --run-id <this run's id>
"""

import argparse
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
# `forge` and `artifact_retention` import bare, from scripts/deploy_tools.
_sys.path.insert(1, str(_Path(__file__).resolve().parents[1]))
from lib.exit_codes import FAILED, OK

import forge
from artifact_retention import LAST_GOOD_TAG, PROD_TAG
from deploy_tools.promote_lib.prune import READ_ERRORS
from deploy_tools.promote_lib.reads import prod_commit

HOLD_VARIABLE = forge.HOLD_VARIABLE
WORKFLOW = "promote.yml"
_SHA = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class ResetReads:
    """Every fact `check` reads, one callable each, raising one of READ_ERRORS on a failure.

    Attributes:
        hold: DEPLOY_HOLD's value, or None when the variable does not exist.
        digest: the digest a tag names, or None when the tag is absent.
        prod_commit: the commit `prod`'s revision annotation names.
        in_progress: the in-progress runs of `promote.yml`, as the Actions API lists them.
    """

    hold: Callable[[], str | None]
    digest: Callable[[str], str | None]
    prod_commit: Callable[[], str | None]
    in_progress: Callable[[], list[dict]]


@dataclass(frozen=True)
class Check:
    """What `check` concluded.

    Attributes:
        verdict: `move`, `none` when `prod` already names `last-good`, or `refused`.
        reason: why, in one sentence.
        prod: `prod`'s digest as read, or empty when it was not read.
        last_good: `last-good`'s digest as read, or empty when it was not read.
    """

    verdict: str
    reason: str
    prod: str = ""
    last_good: str = ""


def check(reads: ResetReads, run_id: str = "") -> Check:
    """Decide whether the reset moves `prod` to `last-good`.

    Args:
        reads: the facts it reads.
        run_id: this run's id, left out of the in-progress runs a refusal names.
    """
    try:
        hold = reads.hold()
    except READ_ERRORS as err:
        return Check("refused", f"{HOLD_VARIABLE} could not be read: {err}")
    if hold is None or not hold.strip() or hold.strip() == "none":
        found = "does not exist" if hold is None else f"reads {hold.strip()!r}"
        return Check(
            "refused",
            f"{HOLD_VARIABLE} {found}, and a reset needs a hold, or the next promote run "
            f"moves prod forward again. Set one first (operator-only): gh variable set "
            f"{HOLD_VARIABLE} --body <reason> -R {forge.REPOSITORY}",
        )
    hold = hold.strip()
    try:
        prod, last_good = reads.digest(PROD_TAG), reads.digest(LAST_GOOD_TAG)
        if prod is None or last_good is None:
            absent = PROD_TAG if prod is None else LAST_GOOD_TAG
            return Check("refused", f"{absent} is absent from the registry")
        if prod == last_good:
            return Check(
                "none", f"prod already names last-good {last_good}", prod, last_good
            )
        named = hold.split()[0]
        if _SHA.fullmatch(named) and reads.digest(f"sha-{named}") == prod:
            return Check(
                "move",
                f"{HOLD_VARIABLE}={hold} names {named}, and sha-{named}'s digest is prod's "
                f"{prod}, so its run has settled; prod moves to last-good {last_good}",
                prod,
                last_good,
            )
    except READ_ERRORS as err:
        return Check("refused", f"a registry read failed: {err}")
    return Check("refused", _in_flight(reads, hold, prod, last_good, run_id))


def _in_flight(
    reads: ResetReads, hold: str, prod: str, last_good: str, run_id: str
) -> str:
    """The refusal for a `prod` that differs from `last-good` under a hold not naming it."""
    try:
        commit = reads.prod_commit() or "absent"
    except READ_ERRORS as err:
        commit = f"unreadable ({err})"
    try:
        runs = [r for r in reads.in_progress() if str(r.get("id")) != run_id]
        listed = ", ".join(
            f"run {r.get('id')} at {str(r.get('head_sha', ''))[:8]}" for r in runs
        )
    except READ_ERRORS as err:
        listed = f"unreadable ({err})"
    return (
        f"prod {prod} is not last-good {last_good}, and {HOLD_VARIABLE}={hold!r} does not "
        f"name prod's commit {commit}, so a verification of it is in flight. In-progress "
        f"{WORKFLOW} runs: {listed or 'none listed'}. Wait for that run's settle, or cancel "
        f"it with `gh run cancel <run-id> -R {forge.REPOSITORY}` (operator-only), then "
        "read last-good again and dispatch again"
    )


def live_reset_reads(f: forge.Forge) -> ResetReads:
    """The reads `promote.py reset-check` makes, against the forge."""
    return ResetReads(
        hold=lambda: f.github.variable(HOLD_VARIABLE),
        digest=f.registry.digest,
        prod_commit=lambda: prod_commit(f.registry.manifest(PROD_TAG)),
        in_progress=lambda: f.github.runs(WORKFLOW, "in_progress"),
    )


def report(result: Check, github_output: str | None) -> int:
    """Print the one line and the reason, append the step outputs, and return the exit code."""
    print(f"verdict={result.verdict} prod={result.prod} last_good={result.last_good}")
    print(f"promote.py reset-check: {result.verdict}: {result.reason}", file=sys.stderr)
    if github_output:
        with open(github_output, "a", encoding="utf-8") as out:
            out.write(
                f"move={str(result.verdict == 'move').lower()}\n"
                f"prod={result.prod}\nlast_good={result.last_good}\n"
            )
    return FAILED if result.verdict == "refused" else OK


def add_parser(commands) -> None:
    """Add the `reset-check` subcommand to `promote.py`'s subparsers."""
    cmd = commands.add_parser(
        "reset-check",
        help="decide whether a reset_to_last_good dispatch moves prod (its read job)",
    )
    cmd.add_argument(
        "--run-id",
        default="",
        help="this run's id, left out of the in-progress runs a refusal names",
    )


def run(f: forge.Forge, args: argparse.Namespace, github_output: str | None) -> int:
    """`promote.py reset-check`."""
    return report(check(live_reset_reads(f), args.run_id), github_output)
