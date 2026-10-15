"""`promote.py land-wait`: wait until a promotion covers a merge commit.

`land.sh` runs it after merging M when the PR carries a tag Flux applies. It waits until a
promotion covers M, never on one `promote` run, because GitHub may evict a queued run and a run
may promote a newer commit. The plan is docs/target-architecture/deploy/d2-first-leaf.md,
*How `land.sh` waits*, and contract C6 in docs/target-architecture/contracts.md.

- It waits on `homelab` and M's changed set (`changed_set.py`), taken between M's
  `digests.json` and the one on the newest published ancestor of M's first parent. A file that
  is missing or unreadable widens the set to every name. `sha-<M>` is usually not published
  yet when the wait starts, so until it is, the set is every name and the read repeats each
  poll.
- It polls every POLL_S seconds until one of three outcomes:
    settled        Every expected Kustomization is Ready, and its
                   `status.lastAppliedOriginRevision` names a commit R that is M or descends
                   from it. One `deploy-annotation` line records it.
    deploy-failed  DEPLOY_HOLD holds a value other than `none`. Cause `promote-held`.
    gave-up        LAND_WAIT_TIMEOUT_S passed. Cause `promote-timeout`.
- It reads the Kustomizations with the read-only `kubectl`, ancestry with
  `git merge-base --is-ancestor`, and DEPLOY_HOLD with `gh variable get`, which is the one
  GitHub call per poll. A read that fails keeps the wait going and never reads as settled.

It prints a line each time what it waits on changes. Its last line is `verdict=<outcome>
cause=<cause> merge=<M> revision=<R> widened=<why the set was every name, or empty>`.

Like `promote.py`, it reaches GitHub, GHCR, the cluster and syslog only through `forge.py` and
`flux_host.py`, and starts no subprocess itself
(scripts/deploy_tools/tests/test_promote_forge_boundary.py).

Run: uv run python scripts/deploy_tools/promote.py land-wait <sha> --services <tag,...>
"""

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
# `forge`, `changed_set` and `flux_host` import bare, from scripts/deploy_tools.
_sys.path.insert(1, str(_Path(__file__).resolve().parents[1]))
from lib.exit_codes import FAILED, LAND_GAVE_UP, OK
from lib.git import git

import forge
from changed_set import ROOT, ChangedSet, changed_set
from flux_host import FluxHost

HOLD_VARIABLE = forge.HOLD_VARIABLE
# The `lastAppliedOriginRevision` form that names a commit: the `--revision` that `flux push
# artifact` writes (contract C6). promote.py reads the same form off `prod`'s manifest.
_REVISION = re.compile(r"master@sha1:([0-9a-f]{40})")


class WaitReadError(RuntimeError):
    """A read outside the forge failed: a git command, or a `digests.json` that is not JSON."""


# Both mean "this fact could not be read".
READ_ERRORS = (WaitReadError, forge.ForgeError)

# Seconds between polls. Each poll makes one authenticated GitHub call, so a landing spends at
# most 120 an hour, well inside a token's 5,000.
POLL_S = 30

# The promote timeout as D2 derives it (d2-first-leaf.md, *Design*, *The timeout is derived,
# not chosen*): the largest Kustomization `timeout` in the bundle, 10m from
# `manifests_rollout_timeout_default`, plus the OCIRepository interval, 1m in
# `ansible/roles/k8s/flux/templates/sync.yaml.j2`, plus the largest `retryInterval`, the 2m
# `scripts/renderer/renderer_lib/bundle.py` writes on each Kustomization. Then the
# `cluster-vars` hop D3.3b adds for a substituting Kustomization: that Kustomization's own 10m
# and one `verify_promotion.REQUEUE_DEPENDENCY_S`. It is the longest wait
# `verify_promotion.promote_timeout` returns for a one-tag bundle (#5155), and
# test_promote_land_wait.py pins the two together. `verify_promotion` imports this module, so
# this one cannot import `promote_timeout` back to derive the figure from M's bundle.
# `land-wait` gives up at twice it (contract C6). The second half covers `promote.yml`'s
# `render` and `publish` jobs, 10m each, which run before any promotion of M can start. It
# does not cover M's `decide` waiting on an earlier commit's verification that is still in
# flight, up to `verify_promotion.IN_FLIGHT_WAIT_S`. Two merges minutes apart can then end the
# wait `gave-up` on a promotion that passes later (#5164).
PROMOTE_TIMEOUT_S = 10 * 60 + 60 + 2 * 60 + 10 * 60 + 30
LAND_WAIT_TIMEOUT_S = 2 * PROMOTE_TIMEOUT_S

# How far back `_published_ancestor` walks M's first-parent history. `publish` pushes every
# green master commit, so the nearest published ancestor is usually M's first parent.
ANCESTOR_WALK_MAX = 50

DIGESTS_FILE = "digests.json"
ANNOTATION_TAG = "deploy-annotation"
HOLD_CAUSE = "promote-held"
TIMEOUT_CAUSE = "promote-timeout"


@dataclass(frozen=True)
class WaitReads:
    """Every fact `land_wait` reads, one callable each.

    A read raises one of READ_ERRORS when it fails. `land_wait` keeps waiting on any such
    failure, except a failed digests read, which widens the set.

    Attributes:
        hold: DEPLOY_HOLD's value, or None when the variable does not exist.
        kustomizations: every Kustomization in flux-system, as `kubectl -o json` lists them.
        is_ancestor: whether the first commit is the second or an ancestor of it, or None when
            the checkout cannot answer yet, such as for an R it has not fetched.
        digests: the parsed `digests.json` of the bundle `sha-<C>` tags, or None when that tag
            is not published.
        published_ancestor: the newest commit on C's first-parent line, C excluded, whose
            `sha-<commit>` tag is published, or None when none is within ANCESTOR_WALK_MAX.
        annotate: writes one syslog line under ANNOTATION_TAG and ignores any failure.
        sleep: waits the given seconds.
        clock: monotonic seconds.
    """

    hold: Callable[[], str | None]
    kustomizations: Callable[[], list[dict]]
    is_ancestor: Callable[[str, str], bool | None]
    digests: Callable[[str], object | None]
    published_ancestor: Callable[[str], str | None]
    annotate: Callable[[str], None]
    sleep: Callable[[float], None]
    clock: Callable[[], float]


@dataclass(frozen=True)
class WaitResult:
    """How `land_wait` ended.

    Attributes:
        verdict: `settled`, `deploy-failed` or `gave-up`.
        cause: HOLD_CAUSE or TIMEOUT_CAUSE beside the last two, "" beside `settled`.
        detail: why, in one sentence.
        revision: R, the commit `homelab` applied, for `settled`.
        widened: why the set was every name, or "" when the digests decided it.
    """

    verdict: str
    cause: str = ""
    detail: str = ""
    revision: str = ""
    widened: str = ""


def origin_commit(kustomization: dict) -> str | None:
    """The commit `status.lastAppliedOriginRevision` names, or None for any other value.

    Only the `master@sha1:<40 hex>` form names a commit. `status.lastAppliedRevision` holds
    `<tag>@sha256:<digest>`, which no git command takes, so it is never read (contract C6).
    """
    status = kustomization.get("status")
    revision = (
        status.get("lastAppliedOriginRevision") if isinstance(status, dict) else None
    )
    match = _REVISION.fullmatch(revision) if isinstance(revision, str) else None
    return match.group(1) if match else None


def is_ready(kustomization: dict) -> bool:
    """Whether the Kustomization's `Ready` condition reads `True`."""
    status = kustomization.get("status")
    conditions = status.get("conditions") if isinstance(status, dict) else None
    return isinstance(conditions, list) and any(
        isinstance(c, dict) and c.get("type") == "Ready" and c.get("status") == "True"
        for c in conditions
    )


def _name(kustomization: dict) -> str:
    metadata = kustomization.get("metadata")
    name = metadata.get("name") if isinstance(metadata, dict) else None
    return name if isinstance(name, str) else ""


def kustomization_graph(items: list[dict]) -> tuple[dict[str, list[str]], set[str]]:
    """Each Kustomization's `dependsOn` names, and the names that are suspended."""
    depends_on: dict[str, list[str]] = {}
    suspended: set[str] = set()
    for item in items:
        name = _name(item)
        if not name:
            continue
        spec = item.get("spec")
        spec = spec if isinstance(spec, dict) else {}
        deps = spec.get("dependsOn")
        depends_on[name] = [
            d["name"]
            for d in (deps if isinstance(deps, list) else [])
            if isinstance(d, dict) and isinstance(d.get("name"), str)
        ]
        if spec.get("suspend") is True:
            suspended.add(name)
    return depends_on, suspended


def hold_detail(hold: str) -> str:
    """What a DEPLOY_HOLD value says: the held SHA and, for a `failed=` hold, its names."""
    held, _, rest = hold.partition(" ")
    detail = f"{HOLD_VARIABLE} holds {held}"
    if rest.startswith("failed="):
        detail += f", and these failed: {rest.removeprefix('failed=')}"
    elif rest:
        detail += f" ({rest})"
    return (
        f"{detail}. That SHA may contain this merge or predate it. Clearing the hold is "
        f"operator-only: `gh variable set {HOLD_VARIABLE} --body none -R {forge.REPOSITORY}`"
    )


class _Expected:
    """The changed set `land_wait` waits on, read again each poll until `sha-<M>` exists."""

    def __init__(self, reads: WaitReads, merge: str) -> None:
        self._reads = reads
        self._merge = merge
        # True once `sha-<M>` was found or its read failed; the reads are not repeated after.
        self._final = False
        self._new: object | None = None
        self._old: object | None = None
        self._why = f"sha-{merge} is not published yet"

    def _read(self) -> None:
        try:
            new = self._reads.digests(self._merge)
        except READ_ERRORS as err:
            self._final, self._why = True, f"M's {DIGESTS_FILE}: {err}"
            return
        if new is None:
            return
        self._final, self._new = True, new
        try:
            ancestor = self._reads.published_ancestor(self._merge)
            if ancestor is None:
                self._why = (
                    f"no published ancestor within {ANCESTOR_WALK_MAX} commits of M's "
                    "first parent"
                )
                return
            self._old = self._reads.digests(ancestor)
            if self._old is None:
                self._why = f"sha-{ancestor} vanished"
        except READ_ERRORS as err:
            self._why = f"the ancestor's {DIGESTS_FILE}: {err}"

    def changes(self, items: list[dict]) -> ChangedSet:
        """The changed set against the Kustomizations `items` lists, `ROOT` excluded."""
        if not self._final:
            self._read()
        depends_on, suspended = kustomization_graph(items)
        if self._old is None:
            # `changed_set` widens to M's names when M's file was read, else to every
            # Kustomization listed; the reason is this wait's, not the function's.
            cs = changed_set(self._new, None, depends_on, suspended)
            return ChangedSet(cs.names, self._why)
        return changed_set(self._new, self._old, depends_on, suspended)


def _pending(
    reads: WaitReads, merge: str, items: list[dict], expected: frozenset[str]
) -> tuple[list[str], str]:
    """Each expected name not yet settled with why, and R, `homelab`'s commit, when none is."""
    by_name = {_name(item): item for item in items}
    pending: list[str] = []
    revision = ""
    for name in sorted(expected):
        item = by_name.get(name)
        if item is None:
            pending.append(f"{name} (absent)")
            continue
        commit = origin_commit(item)
        if commit is None:
            pending.append(f"{name} (no master@sha1 origin revision)")
            continue
        if not is_ready(item):
            pending.append(f"{name} (not Ready)")
            continue
        covered = reads.is_ancestor(merge, commit)
        if covered is None:
            pending.append(f"{name} (origin {commit[:8]} not fetched yet)")
            continue
        if not covered:
            pending.append(f"{name} (at {commit[:8]}, before this merge)")
            continue
        if name == ROOT:
            revision = commit
    return pending, revision


def land_wait(
    reads: WaitReads,
    merge: str,
    services: list[str],
    timeout_s: int = LAND_WAIT_TIMEOUT_S,
    poll_s: int = POLL_S,
) -> WaitResult:
    """Poll until a promotion covers `merge`, DEPLOY_HOLD holds, or `timeout_s` passes.

    The first poll reads before any sleep, so a hold set before the merge ends the wait there.

    Args:
        reads: the facts it reads.
        merge: M, the merge commit the landing waits on.
        services: the PR's published tags, named in the settled annotation.
        timeout_s: the seconds after which it gives up.
        poll_s: the seconds between polls.
    """
    started = reads.clock()
    expected = _Expected(reads, merge)
    last = ""
    widened = ""
    while True:
        try:
            items = reads.kustomizations()
        except READ_ERRORS as err:
            items, line = None, f"the Kustomizations could not be read: {err}"
        if items is not None:
            cs = expected.changes(items)
            widened = cs.widened
            pending, revision = _pending(reads, merge, items, cs.names | {ROOT})
            if not pending:
                annotate_promotion(reads.annotate, services, revision)
                return WaitResult(
                    "settled",
                    detail=f"{len(cs.names) + 1} Kustomizations applied {revision[:8]}",
                    revision=revision,
                    widened=widened,
                )
            line = f"waiting on {', '.join(pending)}"
            if widened:
                line += f" (every name: {widened})"
        try:
            hold = (reads.hold() or "").strip()
        except READ_ERRORS as err:
            hold, line = "", f"{line}; {HOLD_VARIABLE} could not be read: {err}"
        if hold and hold != "none":
            return WaitResult(
                "deploy-failed", HOLD_CAUSE, hold_detail(hold), widened=widened
            )
        if line != last:
            print(f"  promote.py land-wait: {line}", flush=True)
            last = line
        if reads.clock() - started >= timeout_s:
            return WaitResult(
                "gave-up",
                TIMEOUT_CAUSE,
                f"no promotion covered {merge[:8]} within {timeout_s}s. Look first at the "
                "`promote` workflow's failed `publish` jobs: a publish refusal leaves no "
                "sha-<commit> tag to promote",
                widened=widened,
            )
        reads.sleep(poll_s)


def annotate_promotion(
    annotate: Callable[[str], None], services: list[str], revision: str
) -> None:
    """Record a settled promotion as the deploy annotation `deploy.sh` writes for an apply.

    `promote` runs on a GitHub runner, and the line reaches Grafana only through the host's
    syslog, so `land-wait` writes it on the host. `deploy-failed` and `gave-up` write none.
    """
    annotate(
        f"event=deploy services={','.join(services)} sha={revision[:8]} "
        "result=ok source=promote"
    )


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
        annotate=lambda line: host.log(ANNOTATION_TAG, line),
        sleep=time.sleep,
        clock=time.monotonic,
    )


def report_wait(merge: str, result: WaitResult) -> int:
    """Print the detail and the last line, and return the exit code."""
    if result.detail:
        print(f"  promote.py land-wait: {result.verdict}: {result.detail}")
    print(
        f"verdict={result.verdict} cause={result.cause} merge={merge} "
        f"revision={result.revision} widened={result.widened}"
    )
    if result.verdict == "settled":
        return OK
    return LAND_GAVE_UP if result.verdict == "gave-up" else FAILED


def add_parser(commands, repo: Path) -> None:
    """Add the `land-wait` subcommand to `promote.py`'s subparsers, `repo` its default checkout."""
    waiter = commands.add_parser(
        "land-wait", help="wait until a promotion covers a merge commit (land.sh)"
    )
    waiter.add_argument("merge", help="M, the merge commit to wait on")
    waiter.add_argument(
        "--services",
        default="",
        help="the PR's published tags, comma-separated, named in the settled annotation",
    )
    waiter.add_argument(
        "--repo",
        type=Path,
        default=repo,
        help="the checkout whose history answers each ancestry read",
    )
    waiter.add_argument(
        "--timeout",
        type=int,
        default=LAND_WAIT_TIMEOUT_S,
        help="seconds before it gives up (default: twice the promote timeout)",
    )
