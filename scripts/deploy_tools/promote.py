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
           revert rule below is on and the hold is a `failed=` hold.
  current  No commit after `prod`'s is eligible, or a verification never settled, so nothing
           moves. A walk whose every candidate loses an undeclared object ends here too.
  error    A read failed or answered something no rule covers. Nothing moves.

VERIFICATION (contract C2). While `prod` and `last-good` name different digests, a run's
`verify` is still waiting, so `decide` waits too (`promote_lib/verify.py:wait_settled`). Once T is
chosen, it pulls `prod`'s bundle and `sha-<T>`'s before `prod` moves, and writes them under
`--bundle-dir` for `verify`; a failed pull is `error`. The revert rule stays off while
REVERT_RULE is False. `promote_lib/verify.py`'s docstring states all three.

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
when `tag` must tag `prior_digest` as `last-good` first; and `clears_hold`.

PRUNE. `prune` deletes old artifact versions, once a day from `prune-artifacts.yml`.
`promote_lib/prune.py` holds it, and its docstring states what it reads.

VERIFY. `verify T` prints `good` or `failed` for the `settle` job (`promote_lib/verify.py`).
`ci-verdict SHA` prints a commit's CI verdict, for the render job's re-check of a dispatched sha.

LAND-WAIT. `land-wait M` waits until a promotion covers merge commit M, for `land.sh`
(`promote_lib/wait.py`, whose docstring states the three outcomes and what each reads).

CHECK-BUNDLE. `check-bundle DIR` runs the publish census and the source, host and no-stub gates
before any push, and `published-tags` prints the render step's `--role` list. Both read the
checkout alone (`bundle_census.py`, `bundle_gates.py`).

Exit codes:
  0   decide: promote, held or current. prune: every read, delete and verify succeeded.
      land-wait: settled. check-bundle: the census passes. published-tags: printed.
      verify: good. ci-verdict: pass.
  1   decide: error, and nothing moves. prune: a read, a delete or a verify failed.
      land-wait: deploy-failed, a hold. check-bundle: the census or a gate failed, one line
      each.
      verify: failed, or no verdict. ci-verdict: anything but pass.
  64  usage error
  75  land-wait: gave-up, the wait reached its timeout.

Every GitHub and GHCR call goes through `forge.py`, and this module starts no subprocess
(scripts/deploy_tools/tests/test_promote_forge_boundary.py).

Run: uv run python scripts/deploy_tools/promote.py decide --trigger <sha>
     uv run python scripts/deploy_tools/promote.py prune
     uv run python scripts/deploy_tools/promote.py land-wait <sha> --services <tag,...>
     uv run python scripts/deploy_tools/promote.py check-bundle out
     uv run python scripts/deploy_tools/promote.py verify <sha> --bundle-dir <dir>
"""

import argparse
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import removes_object, yaml_fast
from lib.cli_help import ArgumentParser, error
from lib.exit_codes import FAILED, OK
from lib.git import git
from lib.repo_paths import GITOPS_DEPLOY_FILES, REPO

import bundle_census
import forge
import object_loss
from artifact_retention import PROD_TAG
from deploy_tools.promote_lib import verify, wait
from deploy_tools.promote_lib.prune import READ_ERRORS, ReadError, prune

# The deployer's own CI reduction, so a promotion and a tick agree on what green means.
# deploy_git imports nothing, unlike deploy_logic, which re-exports the whole deployer.
sys.path.insert(0, str(GITOPS_DEPLOY_FILES))
from deployer_lib.deploy_git import ci_verdict

# The deployer's required check-run names, as one literal so a `git grep` for the path or the
# key finds this reader when H7.2e moves the key to group_vars.
CI_CONTEXTS_SOURCE = "ansible/roles/setup/gitops_deploy/defaults/main.yml"
CI_CONTEXTS_KEY = "gitops_deploy_ci_contexts"

HOLD_VARIABLE = forge.HOLD_VARIABLE
MASTER_REF = "origin/master"

# The revert rule's switch (contract C2). Open question 11 of
# docs/target-architecture/deploy/d2-first-leaf.md asks whether it turns on, and only the
# operator rules on that, in a one-line PR of its own. Every D2 step assumes it is off.
REVERT_RULE = False

# How many commits a first run's walk examines, newest first, so a walk with no `prod` stops
# short of master's root. At about 124 merges a day (gitops_deploy_ci_ancestor_walk_max's
# comment) 50 is several hours of merges, so the commit that triggered the run is inside it.
FIRST_RUN_WALK_MAX = 50

# `flux push artifact --revision master@sha1:<sha>` writes this annotation on the manifest.
REVISION_ANNOTATION = "org.opencontainers.image.revision"
_REVISION = re.compile(r"master@sha1:([0-9a-f]{40})")


@dataclass(frozen=True)
class Reads:
    """Every fact `decide` reads, one callable each, so a read happens only when needed.

    Each callable returns a value, returns None for absent where absence means something, or
    raises one of READ_ERRORS.

    Attributes:
        hold: DEPLOY_HOLD's value, or None when the variable does not exist.
        contexts: the required check-run names.
        prod: the commit `prod` names, or None when the tag does not exist.
        tip: the commit master's tip names.
        is_ancestor: whether the first commit is an ancestor of the second, or equal to it.
        walk: the commits after the first one (all of history when it is None) up to and
            including the second, newest first.
        tag: the digest of `sha-<C>`, or None when the tag does not exist.
        check_runs: GitHub's check-runs for C.
        verify: returns when the signature on a digest verifies, and raises otherwise.
        digest: the digest a tag such as `prod` or `last-good` names, or None when absent.
        bundle: the files of the bundle a digest names, or None when it is absent.
        declared: the objects a `Removes-Object` line declares on the commits after the
            first one up to and including the second.
        sleep: waits the given seconds, between polls of a verification in flight.
        clock: monotonic seconds.
    """

    hold: Callable[[], str | None]
    contexts: Callable[[], list[str]]
    prod: Callable[[], str | None]
    tip: Callable[[], str]
    is_ancestor: Callable[[str, str], bool]
    walk: Callable[[str | None, str], list[str]]
    tag: Callable[[str], str | None]
    check_runs: Callable[[str], list[dict]]
    verify: Callable[[str], None]
    digest: Callable[[str], str | None]
    bundle: Callable[[str], dict[str, bytes] | None]
    declared: Callable[[str, str], frozenset[str]]
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic


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
        bundles: `prod`'s and T's bundle files by `prod` and `target`, for `--bundle-dir`.
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
    bundles: dict[str, dict[str, bytes] | None] = field(default_factory=dict)


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
            return _promotion(
                reads,
                candidate,
                digest,
                prod,
                prod_digest,
                last_good,
                held,
                target_bundle,
                prod_bundle,
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
    held: tuple | None,
    target_bundle: dict[str, bytes] | None,
    prod_bundle: dict[str, bytes] | None,
) -> Decision:
    """The `promote` decision for T, with the two bundle pulls `verify` reuses.

    Both pulls happen before `prod` moves, so a failed pull promotes nothing. A bundle the
    object-loss check already pulled is reused rather than pulled again.

    Raises:
        ReadError: T's bundle, or `prod`'s, vanished between its HEAD and its pull.
    """
    target_bundle = target_bundle or _pull(reads, digest, f"sha-{target}")
    if prod_bundle is None and prod_digest is not None:
        prod_bundle = _pull(reads, prod_digest, "prod's bundle")
    seed = last_good is None and prod_digest is not None
    return Decision(
        "promote",
        target=target,
        digest=digest,
        prior=prod or "",
        prior_digest=prod_digest or "",
        reset_digest=last_good or prod_digest or "",
        seed=seed,
        clears_hold=held is not None,
        bundles={"prod": prod_bundle, "target": target_bundle},
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


def read_ci_contexts(path: Path) -> list[str]:
    """The `gitops_deploy_ci_contexts` list in the YAML file at `path`.

    Raises:
        ReadError: the file is unreadable, or the key is missing or is anything but a
            non-empty list of non-empty strings. A Jinja reference such as
            `"{{ ci_required_contexts }}"` is a string, so it raises too.
    """
    try:
        data = yaml_fast.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as err:
        raise ReadError(f"{path}: {err}") from err
    value = data.get(CI_CONTEXTS_KEY) if isinstance(data, dict) else None
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(name, str) and name.strip() for name in value)
    ):
        raise ReadError(
            f"{CI_CONTEXTS_KEY} in {path} must be a non-empty list of non-empty strings, "
            f"not {value!r}"
        )
    return value


def prod_commit(manifest: dict | None) -> str | None:
    """The commit a `prod` manifest's revision annotation names, or None for no manifest.

    Raises:
        ReadError: the manifest has no annotation, or one not of the form
            `master@sha1:<40 hex>`.
    """
    if manifest is None:
        return None
    annotations = manifest.get("annotations")
    revision = (
        annotations.get(REVISION_ANNOTATION) if isinstance(annotations, dict) else None
    )
    match = _REVISION.fullmatch(revision) if isinstance(revision, str) else None
    if match is None:
        raise ReadError(
            f"prod's manifest has no {REVISION_ANNOTATION} of the form master@sha1:<sha>, got {revision!r}"
        )
    return match.group(1)


def _git(repo: Path, *args: str) -> str:
    proc = git(*args, cwd=repo, check=False)
    if proc.returncode != 0:
        raise ReadError(
            f"git {' '.join(args)} exited {proc.returncode}: {proc.stderr.strip()}"
        )
    return proc.stdout


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    proc = git(
        "merge-base", "--is-ancestor", ancestor, descendant, cwd=repo, check=False
    )
    if proc.returncode in (0, 1):
        return proc.returncode == 0
    # 128 is an object a shallow checkout lacks, or a name that resolves to nothing.
    raise ReadError(
        f"git merge-base --is-ancestor {ancestor} {descendant} exited {proc.returncode}: "
        f"{proc.stderr.strip()}"
    )


def _walk(repo: Path, prod: str | None, tip: str) -> list[str]:
    # First-parent: master is squash-merged, and a merged branch's own commits were never
    # published. Ancestry-path keeps only commits that descend from prod's.
    args = ["rev-list", "--first-parent", tip]
    if prod is not None:
        args = ["rev-list", "--first-parent", "--ancestry-path", f"{prod}..{tip}"]
    return _git(repo, *args).split()


def live_reads(f: forge.Forge, repo: Path, master_ref: str = MASTER_REF) -> Reads:
    """The reads `promote.py decide` makes, against the forge and the checkout at `repo`."""
    return Reads(
        hold=lambda: f.github.variable(HOLD_VARIABLE),
        contexts=lambda: read_ci_contexts(repo / CI_CONTEXTS_SOURCE),
        prod=lambda: prod_commit(f.registry.manifest(PROD_TAG)),
        tip=lambda: _git(
            repo, "rev-parse", "--verify", f"{master_ref}^{{commit}}"
        ).strip(),
        is_ancestor=lambda a, b: _is_ancestor(repo, a, b),
        walk=lambda prod, tip: _walk(repo, prod, tip),
        tag=lambda sha: f.registry.digest(f"sha-{sha}"),
        check_runs=f.github.check_runs,
        verify=f.cosign.verify,
        digest=f.registry.digest,
        bundle=f.registry.bundle,
        declared=lambda prod, t: removes_object.declared(
            _git(repo, "log", "--format=%B", f"{prod}..{t}")
        ),
    )


def hand_off(decision: Decision, bundle_dir: Path | None) -> Decision:
    """Write a `promote` decision's two bundles under `bundle_dir` for the `verify` job.

    A write that fails turns the decision into `error`, so `prod` never moves without them.
    """
    if decision.verdict != "promote" or bundle_dir is None:
        return decision
    try:
        target = decision.bundles.get("target")
        if target is None:
            raise ValueError("decide pulled no bundle for T")
        verify.write_handoff(bundle_dir, decision.bundles.get("prod"), target)
    except (OSError, ValueError) as err:
        return Decision("error", reason=f"--bundle-dir {bundle_dir}: {err}")
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
            )
    return FAILED if decision.verdict == "error" else OK


def ci_check(f: forge.Forge, repo: Path, sha: str) -> int:
    """Print one commit's CI verdict, as `decide` reduces it, and return 0 only for `pass`.

    The render job runs it on a dispatched `sha`, which carries no CI conclusion of its own.
    """
    try:
        verdict = ci_verdict(
            f.github.check_runs(sha),
            frozenset(read_ci_contexts(repo / CI_CONTEXTS_SOURCE)),
        )
    except READ_ERRORS as err:
        error(f"ci-verdict {sha}: {err}")
        return FAILED
    print(f"sha={sha} ci={verdict}")
    return OK if verdict == "pass" else FAILED


def main(
    argv: list[str] | None = None,
    f: forge.Forge | None = None,
    now: datetime | None = None,
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
    wait.add_parser(commands, REPO)
    bundle_census.add_parsers(commands)
    args = parser.parse_args(argv)
    if args.command in bundle_census.COMMANDS:
        return bundle_census.run(args)
    if f is None:
        f = forge.connect(os.environ)
    if args.command == "land-wait":
        services = [tag for tag in args.services.split(",") if tag]
        reads = wait.live_wait_reads(f, args.repo)
        result = wait.land_wait(reads, args.merge, services, args.timeout)
        return wait.report_wait(args.merge, result)
    if args.command == "prune":
        return prune(
            f,
            now or datetime.now(UTC),
            prod_commit,
            lambda a, b: _is_ancestor(args.repo, a, b),
        )
    if args.command == "ci-verdict":
        return ci_check(f, args.repo, args.sha)
    output = os.environ.get("GITHUB_OUTPUT")
    if args.command == "verify":
        return verify.run(f, args, output)
    try:
        decision = hand_off(
            decide(live_reads(f, args.repo, args.master_ref)), args.bundle_dir
        )
    except Exception as err:
        # The workflow reads the one line and the outputs, so a failure no read anticipated
        # still has to print `error` rather than a traceback alone.
        error(f"unexpected failure: {err!r}")
        decision = Decision("error", reason="unexpected failure")
    return report(args.trigger, decision, os.environ.get("GITHUB_OUTPUT"))


if __name__ == "__main__":
    sys.exit(main())
