"""The live reads `promote.py land-wait` makes, against the forge, the host and the checkout.

`land_wait` takes a `wait.WaitReads`, so a test replaces one field and the wait never learns
where a fact came from. `live_wait_reads` builds the one the CLI runs. Each read raises
`wait.WaitReadError` or `forge.ForgeError` when it fails, and `wait.py`'s docstring states what
`land_wait` does with each.
"""

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
# `forge`, `chained_timeout` and `flux_host` import bare, from scripts/deploy_tools.
_sys.path.insert(1, str(_Path(__file__).resolve().parents[1]))
from lib.git import git

import forge
from artifact_retention import LAST_GOOD_TAG, PROD_TAG
from chained_timeout import VERIFY_CAP_S
from deploy_tools.kustomizations import REVISION as _REVISION
from deploy_tools.promote_lib import reboot_window
from deploy_tools.promote_lib.wait import (
    ANCESTOR_WALK_MAX,
    ANNOTATION_TAG,
    DIGESTS_FILE,
    HOLD_VARIABLE,
    REVISION_ANNOTATION,
    SOAK_CONTEXT,
    WaitReadError,
    WaitReads,
)
from flux_host import FluxHost


def _wait_is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool | None:
    """`git merge-base --is-ancestor`, fetching origin and answering None on any other exit."""
    proc = git(
        "merge-base", "--is-ancestor", ancestor, descendant, cwd=repo, check=False
    )
    if proc.returncode in (0, 1):
        return proc.returncode == 0
    # 128 is most often an R this checkout has not fetched yet.
    git("fetch", "-q", "origin", cwd=repo, check=False)
    return None


def _bundle_digests(f: forge.Forge, sha: str) -> object | None:
    """The parsed `digests.json` of `sha-<sha>`, or None when that tag is not published."""
    files = f.registry.bundle(f"sha-{sha}")
    if files is None:
        return None
    raw = files.get(DIGESTS_FILE)
    if raw is None:
        raise WaitReadError(f"sha-{sha} holds no {DIGESTS_FILE}")
    try:
        return json.loads(raw)
    except ValueError as err:
        raise WaitReadError(f"sha-{sha}'s {DIGESTS_FILE} is not JSON") from err


def _in_flight(f: forge.Forge) -> str | None:
    """The commit `prod` names while it and `last-good` name different digests, else None.

    `decide` reads the same two digests (`verify_promotion.wait_settled`), so this is in flight
    exactly when `decide` would wait.
    """
    tagged = f.registry.tagged(PROD_TAG)
    last_good = f.registry.digest(LAST_GOOD_TAG)
    if tagged is None or last_good is None or tagged[1] == last_good:
        return None
    annotations = tagged[0].get("annotations")
    revision = (
        annotations.get(REVISION_ANNOTATION) if isinstance(annotations, dict) else None
    )
    match = _REVISION.fullmatch(revision) if isinstance(revision, str) else None
    if match is None:
        raise WaitReadError(
            f"{PROD_TAG}'s manifest has no {REVISION_ANNOTATION} of the form "
            f"master@sha1:<sha>, got {revision!r}"
        )
    return match.group(1)


def _soak_status(f: forge.Forge, sha: str) -> str | None:
    found = f.github.status(sha, SOAK_CONTEXT)
    return None if found is None else found[0]


def _published_ancestor(f: forge.Forge, repo: Path, sha: str) -> str | None:
    args = ("rev-list", "--first-parent", f"--max-count={ANCESTOR_WALK_MAX}", f"{sha}^")
    proc = git(*args, cwd=repo, check=False)
    if proc.returncode != 0:
        raise WaitReadError(
            f"git {' '.join(args)} exited {proc.returncode}: {proc.stderr.strip()}"
        )
    for candidate in proc.stdout.split():
        if f.registry.digest(f"sha-{candidate}") is not None:
            return candidate
    return None


def live_wait_reads(
    f: forge.Forge, repo: Path, host: FluxHost | None = None
) -> WaitReads:
    """The reads `promote.py land-wait` makes, against the forge, the host and `repo`."""
    host = host or FluxHost()
    return WaitReads(
        hold=lambda: f.github.variable(HOLD_VARIABLE),
        kustomizations=host.kustomizations,
        is_ancestor=lambda a, b: _wait_is_ancestor(repo, a, b),
        digests=lambda sha: _bundle_digests(f, sha),
        published_ancestor=lambda sha: _published_ancestor(f, repo, sha),
        in_flight=lambda: _in_flight(f),
        soak=lambda sha: _soak_status(f, sha),
        reboot_window=lambda: reboot_window.remaining(
            reboot_window.read_schedule(repo), VERIFY_CAP_S, datetime.now(UTC)
        ),
        annotate=lambda line: host.log(ANNOTATION_TAG, line),
        sleep=time.sleep,
        clock=time.monotonic,
    )
