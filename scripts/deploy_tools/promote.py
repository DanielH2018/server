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

A SUSPENDED TAG TAKES NO CHANGE (D5.1d). A suspended tag applies nothing until its resume, and
`cross_plane_order.py` refuses a PR that changes one. Master's status-check rule is not strict,
so a PR whose checks ran before the suspend merged can still merge after it. So when T's `own`
digest differs from `last-good`'s for a tag `digests.json` marks `suspended` in both, `decide`
holds and writes DEPLOY_HOLD as `<T> suspended-changed=<tag,...>`. The value never takes the
`failed=` form, so the revert rule never passes it, and `suspended_hold` hands it to `hold`,
which posts it. The way out is a commit that reverts the change or resumes the tag, and then the
operator's clear. A clear without one holds again here.

ALREADY-FAILING DEPENDENTS (H1.1i). Before `prod` moves, `decide` records the newest
`flux/<name>` status on `prod`'s commit for each name in T's changed set, and writes the record
under `--bundle-dir` beside the bundles. It reads the statuses only when the changed set holds
a dependent, and a failed read is `error`. `verify` excuses a dependent whose record reads
`failure` (`promote_lib/verify_promotion.py`).

Each read has three outcomes: a value, absent, or an error. Any error is the verdict `error`.
The rules for each read are in docs/target-architecture/deploy/d1-flux-installed.md,
*Decisions*, *Each read has three outcomes*. The ones a reader of this file asks about:
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

OUTPUT. One line on stdout, `trigger=<sha> verdict=<verdict> target=<sha or empty>`. The
reason for a `held`, `current` or `error` goes to stderr. When `$GITHUB_OUTPUT` is set, the
step outputs are appended to it: `verdict`, `target` and `digest`; `prior` and
`prior_digest`, `prod`'s commit and digest before the move, which `tag` re-reads before it
moves anything; `reset_digest`, where a failed verification resets `prod` to; `seed`, `true`
when `tag` must tag `prior_digest` as `last-good` first; `clears_hold`; and `no_reset`, the
comma-joined forward-only tags under which `settle` holds without a reset; `stateful`, the
snapshot roles under which `settle` writes the stateful hold; and `suspended_hold`.

PRUNE. `prune` deletes old artifact versions, once a day from `prune-artifacts.yml`.
`promote_lib/prune.py` holds it, and its docstring states what it reads.

VERIFY. `verify T` prints `good` or `failed` for the `settle` job (`promote_lib/verify_promotion.py`).
`ci-verdict SHA` prints a commit's CI verdict, for the render job's re-check of a dispatched sha.

LAND-WAIT. `land-wait M` waits until a promotion covers merge commit M, for `land.sh`
(`promote_lib/wait.py`, whose docstring states the three outcomes and what each reads).

CHECK-BUNDLE. `check-bundle DIR` runs the publish census and the source, host and no-stub gates
before any push, and `published-tags` prints the render step's `--role` list. Both read the
checkout alone (`bundle_census.py`, `bundle_gates.py`).

Exit codes:
  0   decide: promote, held or current. prune: every read, delete and verify succeeded.
      land-wait: settled. check-bundle: the census passes. published-tags: printed.
      verify: good. ci-verdict: pass. hold: every message posted.
  1   decide: error, and nothing moves, a failed write of the gate's hold included. prune: a read, a delete or a verify failed.
      land-wait: deploy-failed, a hold, or suspended. check-bundle: the census or a gate failed, one line
      each.
      verify: failed, or no verdict. ci-verdict: anything but pass. hold: the record is
      unreadable, or a message was not posted.
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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.cli_help import ArgumentParser, error
from lib.exit_codes import FAILED, OK
from lib.repo_paths import GITOPS_DEPLOY_FILES, REPO

import bundle_census
import forge
from changed_set import suspended_changed
import object_loss
from deploy_tools.promote_lib import (
    forward_only,
    stateful,
    verify_promotion as verify,
    wait,
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

# The deployer's own CI reduction, so a promotion and a tick agree on what green means.
# deploy_git imports nothing, unlike deploy_logic, which re-exports the whole deployer.
sys.path.insert(0, str(GITOPS_DEPLOY_FILES))
from deployer_lib.deploy_git import ci_verdict

# The revert rule's switch (contract C2). Open question 11 of
# docs/target-architecture/deploy/d2-first-leaf.md asks whether it turns on, and only the
# operator rules on that, in a one-line PR of its own. Every D2 step assumes it is off.
REVERT_RULE = False

# How many commits a first run's walk examines, newest first, so a walk with no `prod` stops
# short of master's root. At about 124 merges a day (gitops_deploy_ci_ancestor_walk_max's
# comment) 50 is several hours of merges, so the commit that triggered the run is inside it.
FIRST_RUN_WALK_MAX = 50


@dataclass(frozen=True)
class Decision:
    """What `decide` concluded.

    Attributes:
        verdict: `promote`, `held`, `current` or `error`.
        target: T for `promote`, empty otherwise.
        digest: the digest of `sha-<T>` for `promote`, empty otherwise.
        reason: why, for every verdict but `promote`.
        prior: `prod`'s commit before the move, empty on the first run.
        prior_digest: `prod`'s digest before the move, empty on the first run.
        reset_digest: what a failed verification resets `prod` to: `last-good`'s digest,
            or `prod`'s when `seed` tags it `last-good` first. Empty on the first run.
        seed: `last-good` is absent, so the `tag` job tags `prod`'s digest with it first.
        clears_hold: the revert rule chose T, so a good verdict sets DEPLOY_HOLD to `none`.
        no_reset: the forward-only tags T changes against `last-good`. When any is named, a
            failed verification writes DEPLOY_HOLD as T's SHA alone and leaves `prod` on T.
        stateful: the snapshot roles T changes against `last-good`. When any is named, a
            failed verification writes DEPLOY_HOLD as `<T> stateful=<names>` and leaves
            `prod` on T, whatever `no_reset` says.
        stateful_record: what the hold's post prints (`stateful.record`).
        hold: what `decide` writes to DEPLOY_HOLD itself: T's SHA on a `held` from the
            forward-only gate or the backstop, `<T> suspended-changed=<tags>` on one from the
            suspended-tag check, else empty.
        bundles: `prod`'s and T's bundle files by `prod` and `target`, for `--bundle-dir`.
        recorded: the newest status on `prod`'s commit for each name in T's changed set, by
            name, for `--bundle-dir`. Empty when the set holds no dependent.
    """

    verdict: str
    target: str = ""
    digest: str = ""
    reason: str = ""
    prior: str = ""
    prior_digest: str = ""
    reset_digest: str = ""
    seed: bool = False
    clears_hold: bool = False
    no_reset: tuple[str, ...] = ()
    stateful: tuple[str, ...] = ()
    stateful_record: dict = field(default_factory=dict)
    hold: str = ""
    bundles: dict[str, dict[str, bytes] | None] = field(default_factory=dict)
    recorded: dict[str, str] = field(default_factory=dict)


def decide(reads: Reads, revert_rule: bool = REVERT_RULE) -> Decision:
    """Pick the commit `prod` moves to, reading each fact only when the decision needs it.

    The walk stops at the first eligible candidate, so a run reads one `sha-<C>` digest per
    candidate it examines and lists no tags. A first run, with no `prod`, examines at most
    FIRST_RUN_WALK_MAX candidates. It pulls `prod`'s bundle once, and each candidate's that
    passes every other rule, for the object-loss check.
    """
    try:
        settled = verify.wait_settled(
            reads.hold, reads.digest, reads.sleep, reads.clock, revert_rule
        )
        if settled.stop is not None:
            return Decision(settled.stop[0], reason=settled.stop[1])
        held, prod_digest, last_good = settled.held, settled.prod, settled.last_good
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
            return _promotion(
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
            )
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


def _promotion(
    reads: Reads,
    target: str,
    digest: str,
    prod: str | None,
    prod_digest: str | None,
    last_good: str | None,
    lower: str | None,
    held: tuple | None,
    target_bundle: dict[str, bytes] | None,
    prod_bundle: dict[str, bytes] | None,
    last_good_bundle: dict[str, bytes] | None = None,
) -> Decision:
    """The `promote` decision for T, with the two bundle pulls `verify` reuses.

    Both pulls happen before `prod` moves, so a failed pull promotes nothing. A bundle the
    object-loss check already pulled is reused rather than pulled again. It also names the
    forward-only tags T changes against `last-good` (`forward_only.no_reset_names`), which
    reads `last-good`'s bundle only when `last-good` and `prod` name different digests. And it
    records `prod`'s statuses for `verify`'s excuse (`verify.prior_statuses`), before the move.
    And it names the snapshot roles T changes against the same bundle (`stateful.record`).

    Raises:
        ReadError: T's bundle, or `prod`'s, vanished between its HEAD and its pull, or a
            snapshot Job in T's bundle is unreadable.
        forge.ForgeError: the read of `prod`'s statuses failed.
    """
    target_bundle = target_bundle or _pull(reads, digest, f"sha-{target}")
    if prod_bundle is None and prod_digest is not None:
        prod_bundle = _pull(reads, prod_digest, "prod's bundle")
    seed = last_good is None and prod_digest is not None
    base_bundle = prod_bundle
    if last_good is not None and last_good != prod_digest:
        base_bundle = last_good_bundle or _pull(reads, last_good, "last-good's bundle")
    # A first run has no `last-good`, which therefore lacks every tag T carries.
    base = verify.bundle_digests(base_bundle) if base_bundle is not None else {}
    flagged = reads.forward_only(target)
    if lower is not None:
        flagged |= reads.forward_only(lower)
    target_digests = verify.bundle_digests(target_bundle)
    no_reset = forward_only.no_reset_names(
        forward_only.changed_tags(target_digests, base), flagged
    )
    record = stateful.record(
        target_bundle,
        target_digests,
        verify.bundle_digests(base_bundle) if base_bundle is not None else None,
        digest,
    )
    recorded: dict[str, str] = {}
    if prod is not None and prod_bundle is not None:
        recorded = verify.prior_statuses(
            target_bundle, prod_bundle, lambda: reads.statuses(prod)
        )
        if recorded:
            states = ", ".join(f"flux/{n}={s}" for n, s in sorted(recorded.items()))
            print(
                f"promote.py: recorded on prod's commit {prod}: {states}",
                file=sys.stderr,
            )
    return Decision(
        "promote",
        target=target,
        digest=digest,
        prior=prod or "",
        prior_digest=prod_digest or "",
        reset_digest=last_good or prod_digest or "",
        seed=seed,
        clears_hold=held is not None,
        no_reset=tuple(sorted(no_reset)),
        stateful=tuple(record["stateful"]),
        stateful_record=record,
        bundles={"prod": prod_bundle, "target": target_bundle},
        recorded=recorded,
    )


def _pull(reads: Reads, digest: str, what: str) -> dict[str, bytes]:
    """The files of the bundle at `digest`.

    Raises:
        ReadError: the registry no longer holds it, so it vanished after its HEAD.
    """
    files = reads.bundle(digest)
    if files is None:
        raise ReadError(f"{what} at {digest} vanished before its pull")
    return files


def hand_off(decision: Decision, bundle_dir: Path | None) -> Decision:
    """Write a `promote` decision's two bundles and its stateful record under `bundle_dir`.

    `verify` reads the bundles, and `promote.py hold` reads the record.

    A write that fails turns the decision into `error`, so `prod` never moves without them.
    """
    if decision.verdict != "promote" or bundle_dir is None:
        return decision
    try:
        target = decision.bundles.get("target")
        if target is None:
            raise ValueError("decide pulled no bundle for T")
        verify.write_handoff(
            bundle_dir, decision.bundles.get("prod"), target, decision.recorded
        )
        stateful.write(bundle_dir, decision.stateful_record)
    except (OSError, ValueError) as err:
        return Decision("error", reason=f"--bundle-dir {bundle_dir}: {err}")
    return decision


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
        reads = wait.live_wait_reads(f, args.repo)
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
