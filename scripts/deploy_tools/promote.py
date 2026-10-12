#!/usr/bin/env python3
"""Decide which published master commit the cluster's `prod` tag moves to, and prune old ones.

THE PROBLEM. The `promote` workflow runs after each green CI run on master, and GitHub keeps
only one pending job per concurrency group. Take merges X2 and then X3, where CI(X3) finishes
first: promote(X2) can evict the queued promote(X3), so a decision keyed on the triggering SHA
would promote X2 and strand X3. `decide` therefore ignores the trigger, except to print it. It
walks master from its tip back to `prod`'s commit and takes the first commit T that has a
published `sha-<T>` tag, a green CI verdict and a signature cosign accepts.

THE VERDICTS.
  promote  T was found. `target` is T, and `digest` is the digest of `sha-<T>`.
  held     DEPLOY_HOLD reads something other than `none`. Nothing else is read.
  current  No commit after `prod`'s is eligible, so nothing moves.
  error    A read failed or answered something no rule covers. Nothing moves.

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
step outputs `verdict`, `target` and `digest` are appended to it.

PRUNE. `prune` deletes old artifact versions, once a day from `prune-artifacts.yml`.
`artifact_retention.py` chooses them, and its docstring states the rule. `prune` lists the
package, reads the facts the rule needs, deletes the chosen versions one by one, every
signature after the versions it could sign, then runs `cosign verify` on `prod`'s digest, and
on `last-good`'s when that tag exists. It prints one line per version deleted, then a summary
line. Any failed read deletes nothing.

Exit codes:
  0   decide: promote, held or current. prune: every read, delete and verify succeeded.
  1   decide: error, and nothing moves. prune: a read, a delete or a verify failed.
  64  usage error

Every GitHub and GHCR call goes through `forge.py`. This module imports no HTTP library and
starts no `gh` subprocess (scripts/deploy_tools/tests/test_promote_forge_boundary.py).

Run: uv run python scripts/deploy_tools/promote.py decide --trigger <sha>
     uv run python scripts/deploy_tools/promote.py prune
"""

import argparse
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import yaml_fast
from lib.cli_help import ArgumentParser, error
from lib.exit_codes import FAILED, OK
from lib.git import git
from lib.repo_paths import GITOPS_DEPLOY_FILES, REPO

import forge
from artifact_retention import (
    LAST_GOOD_TAG,
    PROD_TAG,
    Failed,
    PruneFacts,
    Version,
    ancestry_needed,
    select_prunable,
    subjects_needed,
)

# The deployer's own CI reduction, so a promotion and a tick agree on what green means.
# deploy_git imports nothing, unlike deploy_logic, which re-exports the whole deployer.
sys.path.insert(0, str(GITOPS_DEPLOY_FILES))
from deployer_lib.deploy_git import ci_verdict

# The deployer's required check-run names. Named once, as one literal, so a `git grep` for the
# path or the key finds this reader when the key moves (H7.2e moves it to group_vars).
CI_CONTEXTS_SOURCE = "ansible/roles/setup/gitops_deploy/defaults/main.yml"
CI_CONTEXTS_KEY = "gitops_deploy_ci_contexts"

HOLD_VARIABLE = "DEPLOY_HOLD"
MASTER_REF = "origin/master"

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")

# How many commits a first run's walk examines, newest first. Without `prod` the walk would
# otherwise reach master's root, at one `HEAD` and up to one check-runs read per commit. At about
# 124 merges a day (gitops_deploy_ci_ancestor_walk_max's comment) 50 is several hours of merges,
# and `publish` pushes each one, so the commit that triggered the run is well inside it.
FIRST_RUN_WALK_MAX = 50

# `flux push artifact --revision master@sha1:<sha>` writes this annotation on the manifest.
REVISION_ANNOTATION = "org.opencontainers.image.revision"
_REVISION = re.compile(r"master@sha1:([0-9a-f]{40})")


class ReadError(RuntimeError):
    """A read outside the forge failed: the checkout's contexts file, or a git command."""


# Both mean "this fact could not be read", and both make the verdict `error`.
READ_ERRORS = (ReadError, forge.ForgeError)


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


@dataclass(frozen=True)
class Decision:
    """What `decide` concluded.

    Attributes:
        verdict: `promote`, `held`, `current` or `error`.
        target: T for `promote`, empty otherwise.
        digest: the digest of `sha-<T>` for `promote`, empty otherwise.
        reason: why, for every verdict but `promote`.
    """

    verdict: str
    target: str = ""
    digest: str = ""
    reason: str = ""


def decide(reads: Reads) -> Decision:
    """Pick the commit `prod` moves to, reading each fact only when the decision needs it.

    The walk stops at the first eligible candidate, so a run reads one `sha-<C>` digest per
    candidate it examines and lists no tags. A first run, with no `prod`, examines at most
    FIRST_RUN_WALK_MAX candidates.
    """
    try:
        hold = reads.hold()
        if hold is None:
            return Decision("error", reason=f"{HOLD_VARIABLE} does not exist")
        hold = hold.strip()
        if not hold:
            return Decision("error", reason=f"{HOLD_VARIABLE} is empty")
        if hold != "none":
            return Decision("held", reason=f"{HOLD_VARIABLE}={hold}")

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
        for candidate in candidates:
            digest = reads.tag(candidate)
            if digest is None:
                continue
            if ci_verdict(reads.check_runs(candidate), required) != "pass":
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
            return Decision("promote", target=candidate, digest=digest)
    except READ_ERRORS as err:
        return Decision("error", reason=str(err))

    since = (
        f"after prod's commit {prod}"
        if prod
        else f"among master's newest {FIRST_RUN_WALK_MAX}, the first run's limit,"
    )
    return Decision(
        "current", reason=f"no commit {since} is published, green and signed"
    )


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
    )


# -- prune ---------------------------------------------------------------------------------


def parse_version(entry: dict) -> Version:
    """One Packages API version entry as a `Version`.

    Raises:
        ReadError: the entry lacks an integer id, a sha256 digest, a list of string tags or
            a `created_at` with a time zone.
    """
    try:
        tags = entry["metadata"]["container"]["tags"]
        version = Version(
            id=entry["id"],
            digest=entry["name"],
            tags=frozenset(tags),
            created=datetime.fromisoformat(entry["created_at"]),
        )
    except (KeyError, TypeError, ValueError) as err:
        raise ReadError(f"package version {entry!r}: {err!r}") from err
    if (
        not isinstance(version.id, int)
        or not isinstance(version.digest, str)
        or not _DIGEST.fullmatch(version.digest)
        or not isinstance(tags, list)
        or not all(isinstance(tag, str) for tag in tags)
        or version.created.tzinfo is None
    ):
        raise ReadError(f"package version is malformed: {entry!r}")
    return version


def manifest_subject(digest: str, manifest: dict | None) -> str | None:
    """The digest the manifest's `subject` names, or None when it has no `subject`.

    Raises:
        ReadError: the registry has no manifest for a listed version, or its `subject`
            carries no sha256 digest.
    """
    if manifest is None:
        raise ReadError(f"the registry has no manifest for the listed version {digest}")
    subject = manifest.get("subject")
    if subject is None:
        return None
    signed = subject.get("digest") if isinstance(subject, dict) else None
    if not isinstance(signed, str) or not _DIGEST.fullmatch(signed):
        raise ReadError(f"manifest {digest} has a subject with no digest: {subject!r}")
    return signed


def _read[T](read: Callable[[], T]) -> T | Failed:
    """What `read` returns, or `Failed` when it raises one of READ_ERRORS."""
    try:
        return read()
    except READ_ERRORS as err:
        return Failed(str(err))


def _prod(f: forge.Forge) -> tuple[str, str] | None:
    commit = prod_commit(f.registry.manifest(PROD_TAG))
    if commit is None:
        return None
    digest = f.registry.digest(PROD_TAG)
    if digest is None:
        raise ReadError(
            f"{PROD_TAG} disappeared between its manifest read and its HEAD"
        )
    return commit, digest


def prune_facts(f: forge.Forge, repo: Path, now: datetime) -> PruneFacts:
    """Read every fact `select_prunable` needs, stopping at the first read that fails."""
    versions = _read(
        lambda: [parse_version(entry) for entry in f.github.package_versions()]
    )
    prod = _read(lambda: _prod(f))
    last_good = _read(lambda: f.registry.digest(LAST_GOOD_TAG))
    subjects: dict[str, str | None | Failed] = {}
    ancestry: dict[str, bool | Failed] = {}
    if (
        isinstance(versions, Failed)
        or isinstance(prod, Failed)
        or isinstance(last_good, Failed)
        or prod is None
    ):
        return PruneFacts(versions, prod, last_good, ancestry, subjects, now)
    commit = prod[0]
    for digest in sorted(subjects_needed(versions, now)):
        subjects[digest] = _read(
            lambda d=digest: manifest_subject(d, f.registry.manifest(d))
        )
        if isinstance(subjects[digest], Failed):
            break
    for candidate in sorted(ancestry_needed(versions, now)):
        ancestry[candidate] = _read(lambda c=candidate: _is_ancestor(repo, c, commit))
        if isinstance(ancestry[candidate], Failed):
            break
    return PruneFacts(versions, prod, last_good, ancestry, subjects, now)


def prune(f: forge.Forge, repo: Path, now: datetime) -> int:
    """Select, delete one by one, then verify `prod` and `last-good`. Returns the exit code."""
    facts = prune_facts(f, repo, now)
    selection = select_prunable(facts)
    ok = not selection.failed
    if selection.reason:
        print(f"promote.py: prune: {selection.reason}", file=sys.stderr)

    deleted = 0
    for version, why in selection.delete:
        tags = ",".join(sorted(version.tags)) or "untagged"
        try:
            gone = f.github.delete_package_version(version.id)
        except forge.ForgeError as err:
            # A permission error fails every delete alike, so the first one ends the loop.
            print(f"promote.py: prune: {err}", file=sys.stderr)
            ok = False
            break
        deleted += gone
        state = "deleted" if gone else "already gone"
        print(f"{state} {version.id} {version.digest} {tags}: {why}")

    verified = []
    prod_digest = facts.prod[1] if isinstance(facts.prod, tuple) else None
    last_good = facts.last_good if isinstance(facts.last_good, str) else None
    for tag, digest in ((PROD_TAG, prod_digest), (LAST_GOOD_TAG, last_good)):
        if digest is None:
            continue
        try:
            f.cosign.verify(digest)
            verified.append(tag)
        except forge.ForgeError as err:
            print(f"promote.py: prune: {tag} does not verify: {err}", file=sys.stderr)
            ok = False

    listed = len(facts.versions) if isinstance(facts.versions, list) else 0
    # Open question 8 reads this count: whether GHCR lists cosign's bundles as versions.
    signed = sum(isinstance(subject, str) for subject in facts.subjects.values())
    print(
        f"listed={listed} untagged_with_subject={signed} "
        f"selected={len(selection.delete)} deleted={deleted} "
        f"verified={','.join(verified) or 'none'}"
    )
    return OK if ok else FAILED


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
            )
    return FAILED if decision.verdict == "error" else OK


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
        "--repo",
        type=Path,
        default=REPO,
        help="the checkout to read git and the contexts from",
    )
    args = parser.parse_args(argv)
    if f is None:
        f = forge.connect(os.environ)
    if args.command == "prune":
        return prune(f, args.repo, now or datetime.now(UTC))
    try:
        decision = decide(live_reads(f, args.repo, args.master_ref))
    except Exception as err:
        # The workflow reads the one line and the outputs, so a failure no read anticipated
        # still has to print `error` rather than a traceback alone.
        error(f"unexpected failure: {err!r}")
        decision = Decision("error", reason="unexpected failure")
    return report(args.trigger, decision, os.environ.get("GITHUB_OUTPUT"))


if __name__ == "__main__":
    sys.exit(main())
