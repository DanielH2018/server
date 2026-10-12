#!/usr/bin/env python3
"""Decide which published master commit the cluster's `prod` tag moves to.

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
  - A `sha-<C>` HEAD that returns 404 makes C ineligible. A CI verdict of pending or fail
    does too. A failed signature check is an error, so a Sigstore outage stops promotion
    rather than skipping to an older commit.

OUTPUT. One line on stdout, `trigger=<sha> verdict=<verdict> target=<sha or empty>`. The
reason for a `held`, `current` or `error` goes to stderr. When `$GITHUB_OUTPUT` is set, the
step outputs `verdict`, `target` and `digest` are appended to it.

Exit codes:
  0   promote, held or current
  1   error: nothing moves
  64  usage error

Every GitHub and GHCR call goes through `forge.py`. This module imports no HTTP library and
starts no `gh` subprocess (scripts/deploy_tools/tests/test_promote_forge_boundary.py).

Run: uv run python scripts/deploy_tools/promote.py decide --trigger <sha>
"""

import argparse
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import yaml_fast
from lib.cli_help import ArgumentParser, error
from lib.exit_codes import FAILED, OK
from lib.git import git
from lib.repo_paths import GITOPS_DEPLOY_FILES, REPO

import forge

# The deployer's own CI reduction, so a promotion and a tick agree on what green means.
# deploy_git imports nothing, unlike deploy_logic, which re-exports the whole deployer.
sys.path.insert(0, str(GITOPS_DEPLOY_FILES))
from deployer_lib.deploy_git import ci_verdict

# The deployer's required check-run names. Named once, as one literal, so a `git grep` for the
# path or the key finds this reader when the key moves (H7.2e moves it to group_vars).
CI_CONTEXTS_SOURCE = "ansible/roles/setup/gitops_deploy/defaults/main.yml"
CI_CONTEXTS_KEY = "gitops_deploy_ci_contexts"

HOLD_VARIABLE = "DEPLOY_HOLD"
PROD_TAG = "prod"
MASTER_REF = "origin/master"

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
    candidate it examines and lists no tags.
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

        for candidate in reads.walk(prod, tip):
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

    since = f"after prod's commit {prod}" if prod else "on master"
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


def main(argv: list[str] | None = None, f: forge.Forge | None = None) -> int:
    parser = ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
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
