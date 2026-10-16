"""Every fact `promote.py decide` reads, as `Reads`, and the live reads against the forge and git.

`decide` takes a `Reads`, so a test replaces one field and the decision never learns where a
fact came from. `live_reads` builds the one the CLI runs, over `forge.py` and the checkout.
`prod_commit` reads the commit a tag's revision annotation names, for `prod` and `last-good`
here and for `land.sh`'s pin gate. `promote.py` re-exports every public name.
"""

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
# `forge` and `artifact_retention` import bare, from scripts/deploy_tools.
_sys.path.insert(1, str(_Path(__file__).resolve().parents[1]))
from lib import removes_object
from lib.git import git
from validate import cluster_pins
from validate.cluster_pins import Pin

import forge
from artifact_retention import LAST_GOOD_TAG, PROD_TAG
from deploy_tools.promote_lib import forward_only, wait
from deploy_tools.promote_lib.ci_contexts import CI_CONTEXTS_SOURCE, read_ci_contexts
from deploy_tools.promote_lib.prune import ReadError

HOLD_VARIABLE = forge.HOLD_VARIABLE
MASTER_REF = "origin/master"

# `flux push artifact --revision master@sha1:<sha>` writes this annotation on the manifest.
# `land-wait` reads it off `prod` too, so the name lives in `wait`.
REVISION_ANNOTATION = wait.REVISION_ANNOTATION
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
        last_good_commit: the commit `last-good` names, or None when the tag does not exist.
        forward_only: the tags whose role sets `flux_forward_only: true` at a commit.
        pins: each forward-only pin at a commit (`cluster_pins.pins_at`).
        pulls: the PRs GitHub associates with a commit.
        gate_status: the newest `homelab/upgrade-gates` status on a commit as
            `(state, updated_at)`, or None.
        now: the wall-clock time, for the gate status's age bound.
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
    last_good_commit: Callable[[], str | None]
    forward_only: Callable[[str], frozenset[str]]
    pins: Callable[[str], Mapping[str, Pin | None]]
    pulls: Callable[[str], list[dict]]
    gate_status: Callable[[str], tuple[str, datetime] | None]
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic


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


def git_read(repo: Path, *args: str) -> str:
    proc = git(*args, cwd=repo, check=False)
    if proc.returncode != 0:
        raise ReadError(
            f"git {' '.join(args)} exited {proc.returncode}: {proc.stderr.strip()}"
        )
    return proc.stdout


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
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


def walk(repo: Path, prod: str | None, tip: str) -> list[str]:
    # First-parent: master is squash-merged, and a merged branch's own commits were never
    # published. Ancestry-path keeps only commits that descend from prod's.
    args = ["rev-list", "--first-parent", tip]
    if prod is not None:
        args = ["rev-list", "--first-parent", "--ancestry-path", f"{prod}..{tip}"]
    return git_read(repo, *args).split()


def live_reads(
    f: forge.Forge,
    repo: Path,
    master_ref: str = MASTER_REF,
    now: datetime | None = None,
) -> Reads:
    """The reads `promote.py decide` makes, against the forge and the checkout at `repo`.

    `now`, when given, fixes the time the gate status's age is measured from.
    """
    return Reads(
        now=(lambda: now) if now is not None else (lambda: datetime.now(UTC)),
        hold=lambda: f.github.variable(HOLD_VARIABLE),
        contexts=lambda: read_ci_contexts(repo / CI_CONTEXTS_SOURCE),
        prod=lambda: prod_commit(f.registry.manifest(PROD_TAG)),
        tip=lambda: git_read(
            repo, "rev-parse", "--verify", f"{master_ref}^{{commit}}"
        ).strip(),
        is_ancestor=lambda a, b: is_ancestor(repo, a, b),
        walk=lambda prod, tip: walk(repo, prod, tip),
        tag=lambda sha: f.registry.digest(f"sha-{sha}"),
        check_runs=f.github.check_runs,
        verify=f.cosign.verify,
        digest=f.registry.digest,
        bundle=f.registry.bundle,
        declared=lambda prod, t: removes_object.declared(
            git_read(repo, "log", "--format=%B", f"{prod}..{t}")
        ),
        last_good_commit=lambda: prod_commit(f.registry.manifest(LAST_GOOD_TAG)),
        forward_only=lambda sha: forward_only.forward_only_roles(repo, sha),
        pins=lambda sha: cluster_pins.pins_at(sha, repo),
        pulls=f.github.commit_pulls,
        gate_status=lambda sha: f.github.status(sha, forward_only.GATE_STATUS_CONTEXT),
    )
