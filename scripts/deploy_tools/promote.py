#!/usr/bin/env python3
"""Decide which published master commit the cluster's `prod` tag moves to, wait on it, prune.

THE PROBLEM. The `promote` workflow runs after each green CI run on master, and GitHub keeps
only one pending job per concurrency group. Take merges X2 and then X3, where CI(X3) finishes
first: promote(X2) can evict the queued promote(X3), so a decision keyed on the triggering SHA
would promote X2 and strand X3. `decide` therefore ignores the trigger, except to print it. It
walks master from its tip back to `prod`'s commit and takes the first commit T that has a
published `sha-<T>` tag, a green CI verdict and a signature cosign accepts, and whose bundle
loses no object that `prod`'s holds unless a `Removes-Object:` line in (`prod`, T] declares
it (`object_loss.py`).

THE VERDICTS.
  promote  T was found. `target` is T, and `digest` is the digest of `sha-<T>`.
  held     DEPLOY_HOLD reads something other than `none`. Nothing else is read, unless the
           revert rule below is on and the hold is a `failed=` hold. Also when T fails the
           forward-only gate or the backstop, and then `decide` writes T's SHA to DEPLOY_HOLD,
           or when T changes a suspended tag, below.
  current  No commit after `prod`'s is eligible, or a verification never settled, so nothing
           moves. A walk whose every candidate loses an undeclared object ends here too.
  deferred T was found, but the clock is inside T's no-promotion window before the weekly
           reboot, so nothing moves and nothing waits (D5.2b2, `promote_lib/reboot_window.py`).
           `promote.yml`'s `schedule:` run at the window's end promotes master's tip.
  error    A read failed or answered something no rule covers. Nothing moves.

VERIFICATION (contract C2). While `prod` and `last-good` name different digests, a run's
`verify` is still waiting, so `decide` waits too (`promote_lib/verify_promotion.py:wait_settled`). Once T is
chosen, it pulls `prod`'s bundle and `sha-<T>`'s before `prod` moves, and writes them under
`--bundle-dir` for `verify`; a failed pull is `error`. The revert rule stays off while
REVERT_RULE is False. `promote_lib/verify_promotion.py`'s docstring states all three.

FORWARD-ONLY TAGS (H1.1b). Before T is promoted, every commit in (`last-good`, T] that changes
a forward-only pin needs a fresh `homelab/upgrade-gates` status on its PR's head, and
(`prod`, T] may change each pin once. T's `no_reset` names the forward-only tags it changes
against `last-good`, under which a failed verification holds as T's SHA and leaves `prod`.
`promote_lib/forward_only.py`'s docstring states the three rules.

SNAPSHOT ROLES (D5.2b1). Under T's `stateful`, the snapshot roles whose `own` digest T
changes, a failed verification holds as `<T> stateful=<names>` without a reset. `hold T`
posts it after `settle`, from the record `decide` writes (`promote_lib/stateful.py`).

A SUSPENDED TAG TAKES NO CHANGE (D5.1d). A PR whose checks ran before a suspend merged can still
merge after it, since master's status-check rule is not strict. So when T's `own` digest differs
from `last-good`'s for a tag `digests.json` marks `suspended` in both, `decide` holds and writes
DEPLOY_HOLD as `<T> suspended-changed=<tag,...>`, which the revert rule never passes and `hold`
posts. The way out is a commit that reverts the change or resumes the tag, then the clear.

ALREADY-FAILING DEPENDENTS (H1.1i). Before `prod` moves, `decide` records the newest
`flux/<name>` status on `prod`'s commit for each name in T's changed set, and writes the record
under `--bundle-dir` beside the bundles. It reads the statuses only when the changed set holds
a dependent, and a failed read is `error`. `verify` excuses a dependent whose record reads
`failure` (`promote_lib/verify_promotion.py`).

Each read has three outcomes: a value, absent, or an error, and any error is `error`
(d1-flux-installed.md, *Each read has three outcomes*). The ones a reader asks about:
  - DEPLOY_HOLD: `none` is no hold. Any other non-empty value holds, so a mistyped hold
    still holds. Empty or missing is an error, because an empty read looks the same as a
    broken one.
  - The required CI contexts come from the checkout, at the one path in CI_CONTEXTS_SOURCE.
    The value must be a non-empty list of non-empty strings. `ci_verdict` reads an empty set
    as `pass`, and a string would stall every candidate at `pending` with no error.
  - `prod` absent from the registry is the first run, and promotes the newest eligible commit.
    Its walk has no `prod` to stop at, so it examines at most FIRST_RUN_WALK_MAX commits from
    master's tip and reports `current` past them.
  - A `sha-<C>` HEAD that returns 404 makes C ineligible. A CI verdict of pending or fail
    does too. A failed signature check is an error, so a Sigstore outage stops promotion
    rather than skipping to an older commit.

OUTPUT. One line on stdout, `trigger=<sha> verdict=<verdict> target=<sha or empty>`, and any
verdict's reason on stderr. `$GITHUB_OUTPUT` gets `verdict`, `target`, `digest`; `prior` and
`prior_digest`, which `tag` re-reads before it moves anything; `reset_digest`; `seed`, `true`
when `tag` tags `prior_digest` `last-good` first; `clears_hold`; `no_reset` and `stateful`,
under which `settle` holds without a reset; and `suspended_hold`.

THE OTHER SUBCOMMANDS, each stated in its module's docstring:
  prune        deletes old artifact versions, daily from prune-artifacts.yml (`prune.py`).
  verify T     prints `good` or `failed` for `settle` (`verify_promotion.py`).
  ci-verdict   prints a commit's CI verdict, for render's re-check of a dispatched sha.
  land-wait M  waits until a promotion covers merge commit M, for `land.sh` (`wait.py`).
  reset-check  the `reset_to_last_good` dispatch's read job (`reset.py`, D5.2b2).

CHECK-BUNDLE. `check-bundle DIR` runs the publish census and the source, host and no-stub gates
before any push, and `published-tags` prints the render step's `--role` list. Both read the
checkout alone (`bundle_census.py`, `bundle_gates.py`).

Exit codes:
  0   decide: promote, held, current or deferred. prune: every read, delete and verify succeeded.
      land-wait: settled. check-bundle: the census passes. published-tags: printed.
      verify: good. ci-verdict: pass. hold: every message posted. reset-check: a move or none.
  1   decide: error, and nothing moves, a failed write of the gate's hold included. prune: a
      read, a delete or a verify failed. land-wait: deploy-failed, a hold, or suspended.
      check-bundle: the census or a gate failed, one line each.
      verify: failed, or no verdict. ci-verdict: anything but pass. hold: the record is
      unreadable, or a message was not posted. reset-check: a refusal.
  64  usage error
  75  land-wait: gave-up, the wait reached its timeout.

Every GitHub and GHCR call goes through `forge.py`, and this module starts no subprocess
(scripts/deploy_tools/tests/test_promote_forge_boundary.py).

Run: uv run python scripts/deploy_tools/promote.py decide --trigger <sha>
     uv run python scripts/deploy_tools/promote.py land-wait <sha> --services <tag,...>
     uv run python scripts/deploy_tools/promote.py check-bundle out
     uv run python scripts/deploy_tools/promote.py verify <sha> --bundle-dir <dir>
"""

import argparse
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.cli_help import ArgumentParser, error
from lib.exit_codes import FAILED, OK
from lib.repo_paths import HOST_LIB_FILES, REPO

import bundle_census
import forge
from changed_set import suspended_changed
import object_loss
from deploy_tools.promote_lib import forward_only, reset, stateful, supersede
from deploy_tools.promote_lib import verify_promotion as verify, wait, wait_reads
from deploy_tools.promote_lib.decision import (
    Decision as Decision,
    _promotion,
    _pull,
    hand_off as hand_off,
)

# Re-exported, as `promote.<name>`, for the tests and for `land.sh`'s pin gate.
from deploy_tools.promote_lib.ci_contexts import (
    CI_CONTEXTS_SOURCE as CI_CONTEXTS_SOURCE,
    ci_check,
    read_ci_contexts as read_ci_contexts,
)
from deploy_tools.promote_lib.prune import READ_ERRORS, ReadError as ReadError, prune
from deploy_tools.promote_lib.reads import (
    HOLD_VARIABLE,
    MASTER_REF,
    REVISION_ANNOTATION as REVISION_ANNOTATION,
    Reads,
    is_ancestor,
    live_reads,
    prod_commit,
)

# The CI reduction every deploy path shares, so a promotion and a tick agree on what green
# means. It moved to `hosts/lib/` in H7.2e so the deployer can retire without it.
sys.path.insert(0, str(HOST_LIB_FILES))
from ci_verdict import ci_verdict

# The revert rule's switch (contract C2). Open question 11 of
# docs/target-architecture/deploy/d2-first-leaf.md asks whether it turns on, and only the
# operator rules on that, in a one-line PR of its own. Every D2 step assumes it is off.
REVERT_RULE = False

# Whether `decide` may promote T over a verification in flight rather than wait on it (slice
# D7, `promote_lib/supersede.py`). Turning it on is an operator ruling, in a one-line PR of its
# own, as for REVERT_RULE.
SUPERSEDE = False

# How many commits a first run's walk examines, newest first, so a walk with no `prod` stops
# short of master's root. At about 124 merges a day (see `ci_ancestor_walk_max`) 50 is several
# hours of merges, so the commit that triggered the run is inside it.
FIRST_RUN_WALK_MAX = 50


def decide(
    reads: Reads, revert_rule: bool = REVERT_RULE, supersede_on: bool = SUPERSEDE
) -> Decision:
    """Pick the commit `prod` moves to, reading each fact only when the decision needs it.

    The walk stops at the first eligible candidate, so a run reads one `sha-<C>` digest per
    candidate it examines and lists no tags. A first run, with no `prod`, examines at most
    FIRST_RUN_WALK_MAX candidates. It pulls `prod`'s bundle once, and each candidate's that
    passes every other rule, for the object-loss check.

    With `supersede_on`, a verification in flight that `supersede.in_flight` names does not
    wait: T is promoted over it unless `supersede.refusal` or T's forward-only tags or
    snapshot roles refuse, and then `decide` runs again with the wait.
    """
    try:
        flight = supersede.in_flight(reads) if supersede_on else None
        hold_read = "none"
        if flight is not None:
            held, prod_digest, last_good = None, flight.prod, flight.last_good
        else:
            settled = verify.wait_settled(
                reads.hold, reads.digest, reads.sleep, reads.clock, revert_rule
            )
            if settled.stop is not None:
                return Decision(settled.stop[0], reason=settled.stop[1])
            held, prod_digest, last_good = settled.held, settled.prod, settled.last_good
            hold_read = settled.hold
        required = frozenset(reads.contexts())
        prod = reads.prod()
        tip = reads.tip()
        if prod == tip:
            return Decision("current", reason=f"prod already names master's tip {tip}")
        if prod is not None and not reads.is_ancestor(prod, tip):
            return Decision(
                "error",
                reason=f"prod's commit {prod} is not in master's history at {tip}",
            )

        candidates = reads.walk(prod, tip)
        if prod is None:
            candidates = candidates[:FIRST_RUN_WALK_MAX]
        last_good_bundle = prod_bundle = None
        skipped = 0
        for candidate in candidates:
            digest = reads.tag(candidate)
            if digest is None:
                continue
            if ci_verdict(reads.check_runs(candidate), required) != "pass":
                continue
            target_bundle = None
            if held is not None:
                # The revert rule: only a descendant of the held commit whose footprints
                # for the failed names equal last-good's. Each read happens only here.
                held_sha, names = held
                if candidate == held_sha or not reads.is_ancestor(held_sha, candidate):
                    continue
                if last_good_bundle is None and last_good is not None:
                    last_good_bundle = reads.bundle(last_good)
                target_bundle = reads.bundle(digest)
                if not verify.footprints_match(target_bundle, last_good_bundle, names):
                    continue
            reads.verify(digest)
            # The walk only yields descendants. This holds the rule that prod never moves
            # backwards even if the walk is ever changed.
            if prod is not None and (
                candidate == prod or not reads.is_ancestor(prod, candidate)
            ):
                return Decision(
                    "error",
                    reason=f"{candidate} does not descend from prod's commit {prod}",
                )
            if prod is not None and prod_digest is not None:
                target_bundle = target_bundle or _pull(
                    reads, digest, f"sha-{candidate}"
                )
                if prod_bundle is None:
                    prod_bundle = _pull(reads, prod_digest, "prod's bundle")
                skip = object_loss.refusal(
                    prod_bundle,
                    target_bundle,
                    reads.declared(prod, candidate),
                    prod,
                    candidate,
                )
                if skip:
                    print(skip, file=sys.stderr)
                    skipped += 1
                    continue
                # `last-good`'s bundle: `prod`'s while they agree, and when `tag` seeds it.
                if last_good is None or last_good == prod_digest:
                    last_good_bundle = prod_bundle
                elif last_good_bundle is None:
                    last_good_bundle = _pull(reads, last_good, "last-good's bundle")
                if changed := suspended_changed(
                    verify.bundle_digests(last_good_bundle),
                    verify.bundle_digests(target_bundle),
                ):
                    tags = ",".join(changed)
                    return Decision(
                        "held",
                        reason=f"{candidate} changes {tags}, suspended at last-good and "
                        "at it, so the change would apply nothing until the resume. Revert "
                        "it or resume the tag, then clear DEPLOY_HOLD (operator-only); a "
                        "clear without either holds again at the next decide",
                        hold=f"{candidate} suspended-changed={tags}",
                    )
            # The commit `last-good` names: `prod`'s while they agree, and `prod`'s when the
            # `tag` job seeds `last-good` from it.
            lower = (
                prod
                if last_good is None or last_good == prod_digest
                else reads.last_good_commit()
            )
            if problems := forward_only.gate(
                reads, lower, prod, candidate, FIRST_RUN_WALK_MAX
            ):
                return Decision(
                    "held",
                    reason=f"{candidate} fails the forward-only gate: "
                    + "; ".join(problems),
                    hold=candidate,
                )
            why = ""
            if flight is not None:
                why = supersede.refusal(
                    reads, prod, prod_bundle, last_good_bundle, target_bundle
                )
            if not why:
                decision = _promotion(
                    reads,
                    candidate,
                    digest,
                    prod,
                    prod_digest,
                    last_good,
                    lower,
                    held,
                    target_bundle,
                    prod_bundle,
                    last_good_bundle,
                    supersedes=(prod or "") if flight is not None else "",
                    hold_read=hold_read,
                )
                if flight is None or not (decision.no_reset or decision.stateful):
                    return decision
                why = "T names a forward-only tag or a snapshot role"
            print(
                f"promote.py: {candidate} waits rather than supersede {prod}: {why}",
                file=sys.stderr,
            )
            return decide(reads, revert_rule, supersede_on=False)
    except READ_ERRORS as err:
        return Decision("error", reason=str(err))

    if held is not None:
        return Decision(
            "held",
            reason=f"{HOLD_VARIABLE}={held[0]} failed={','.join(sorted(held[1]))}, and no "
            "descendant of it matches last-good's footprints for those names",
        )
    since = (
        f"after prod's commit {prod}"
        if prod
        else f"among master's newest {FIRST_RUN_WALK_MAX}, the first run's limit,"
    )
    losing = f"; {skipped} lost an object no trailer declares" if skipped else ""
    return Decision(
        "current", reason=f"no commit {since} is published, green and signed{losing}"
    )


def write_hold(decision: Decision, f: forge.Forge) -> Decision:
    """Write a `held` decision's DEPLOY_HOLD value; a failed write makes the run `error`.

    The forward-only gate and the backstop write T's SHA alone, and the suspended-tag check
    writes `<T> suspended-changed=<tags>`. The revert rule passes neither form (contracts.md
    C2, *DEPLOY_HOLD's values*).
    """
    if not decision.hold:
        return decision
    try:
        f.github.set_variable(HOLD_VARIABLE, decision.hold)
    except READ_ERRORS as err:
        return Decision(
            "error",
            reason=f"{decision.reason}, and writing {HOLD_VARIABLE} failed: {err}",
        )
    print(f"promote.py: {HOLD_VARIABLE} set to {decision.hold}", file=sys.stderr)
    return decision


def report(trigger: str, decision: Decision, github_output: str | None) -> int:
    """Print the one line, append the step outputs, and return the exit code."""
    print(f"trigger={trigger} verdict={decision.verdict} target={decision.target}")
    if decision.supersedes:
        print(
            f"promote.py: {decision.target} supersedes the verification of "
            f"{decision.supersedes}",
            file=sys.stderr,
        )
    if decision.reason:
        print(f"promote.py: {decision.verdict}: {decision.reason}", file=sys.stderr)
    if github_output:
        with open(github_output, "a", encoding="utf-8") as out:
            out.write(
                f"verdict={decision.verdict}\n"
                f"target={decision.target}\n"
                f"digest={decision.digest}\n"
                f"prior={decision.prior}\n"
                f"prior_digest={decision.prior_digest}\n"
                f"reset_digest={decision.reset_digest}\n"
                f"seed={str(decision.seed).lower()}\n"
                f"clears_hold={str(decision.clears_hold).lower()}\n"
                f"no_reset={','.join(decision.no_reset)}\n"
                f"stateful={','.join(decision.stateful)}\n"
                f"supersedes={decision.supersedes}\n"
                f"hold_read={decision.hold_read}\n"
                f"suspended_hold={decision.hold if stateful.SUSPENDED in decision.hold else ''}\n"
            )
    return FAILED if decision.verdict == "error" else OK


def main(
    argv: list[str] | None = None,
    f: forge.Forge | None = None,
    now: datetime | None = None,
    post: Callable[[str], None] | None = None,
) -> int:
    parser = ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    pruner = commands.add_parser(
        "prune", help="delete the artifact versions no promotion or rollback can reach"
    )
    pruner.add_argument(
        "--repo",
        type=Path,
        default=REPO,
        help="the checkout whose history answers each ancestry read",
    )
    cmd = commands.add_parser("decide", help="print the promotion verdict for this run")
    cmd.add_argument(
        "--trigger",
        default="",
        help="the SHA whose CI run triggered this run; printed, never used to choose",
    )
    cmd.add_argument("--master-ref", default=MASTER_REF, help="the ref to walk from")
    cmd.add_argument(
        "--bundle-dir",
        type=Path,
        help="where a `promote` verdict writes prod's and T's bundles for `verify`",
    )
    ci = commands.add_parser(
        "ci-verdict", help="print a commit's CI verdict, exiting 0 only for pass"
    )
    ci.add_argument("sha", help="the commit whose check-runs are read")
    for sub in (cmd, ci):
        sub.add_argument(
            "--repo",
            type=Path,
            default=REPO,
            help="the checkout to read git and the contexts from",
        )
    verify.add_parser(commands)
    stateful.add_parser(commands)
    reset.add_parser(commands)
    wait.add_parser(commands, REPO)
    bundle_census.add_parsers(commands)
    args = parser.parse_args(argv)
    if args.command in bundle_census.COMMANDS:
        return bundle_census.run(args)
    if args.command == "hold":
        if post is None:
            post = forge.Discord(os.environ.get(stateful.WEBHOOK_ENV, "")).post
        return stateful.run(args, post)
    if f is None:
        f = forge.connect(os.environ)
    if args.command == "land-wait":
        services = [tag for tag in args.services.split(",") if tag]
        reads = wait_reads.live_wait_reads(f, args.repo)
        also = frozenset(name for name in args.also.split(",") if name)
        result = wait.land_wait(reads, args.merge, services, args.timeout, also=also)
        return wait.report_wait(args.merge, result)
    if args.command == "prune":
        return prune(
            f,
            now or datetime.now(UTC),
            prod_commit,
            lambda a, b: is_ancestor(args.repo, a, b),
        )
    if args.command == "ci-verdict":
        return ci_check(f, args.repo, args.sha)
    output = os.environ.get("GITHUB_OUTPUT")
    if args.command == "verify":
        return verify.run(f, args, output)
    if args.command == "reset-check":
        return reset.run(f, args, output)
    try:
        decision = hand_off(
            write_hold(decide(live_reads(f, args.repo, args.master_ref, now)), f),
            args.bundle_dir,
        )
    except Exception as err:
        # The workflow reads the one line and the outputs, so a failure no read anticipated
        # still has to print `error` rather than a traceback alone.
        error(f"unexpected failure: {err!r}")
        decision = Decision("error", reason="unexpected failure")
    return report(args.trigger, decision, os.environ.get("GITHUB_OUTPUT"))


if __name__ == "__main__":
    sys.exit(main())
