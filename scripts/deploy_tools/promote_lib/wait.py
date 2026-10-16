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
- `--also <name,...>` adds names the digests may not show, and every Kustomization that
  depends on one of them, read from the live `dependsOn` graph each poll. `land.sh` passes
  `cluster-vars` for a PR that changes a cluster file (D3.4): a change to
  `cluster-vars.source` alone moves no digest.
- It polls every POLL_S seconds until one of four outcomes:
    settled        Every expected Kustomization is Ready, and its
                   `status.lastAppliedOriginRevision` names a commit R that is M or descends
                   from it. One `deploy-annotation` line records it.
    deploy-failed  DEPLOY_HOLD holds a value other than `none`. Cause `promote-held`.
    gave-up        LAND_WAIT_TIMEOUT_S passed, or the later deadline below. Cause
                   `promote-timeout`.
    suspended      One of `--services` is `suspended` in M's `digests.json`, so Flux applies
                   nothing for it until a PR resumes it (D5.1d). It ends the wait at the
                   first poll that reads M's file, rather than polling to `gave-up`. The
                   suspend PR itself ends this way. Cause `promote-suspended`. A tag M
                   changed while suspended at both ends is left to `decide`'s hold.
- While a verification of an earlier commit P is in flight, M's `decide` waits on it rather
  than promoting, for up to IN_FLIGHT_WAIT_S. So each poll that reads `prod` differing from
  `last-good` at a strict ancestor of M moves the deadline to LAND_WAIT_TIMEOUT_S past that
  poll. It stops moving IN_FLIGHT_WAIT_S after P was first read in flight, because `decide`
  gives up there too (#5164).
- It reads the Kustomizations with the read-only `kubectl`, ancestry with
  `git merge-base --is-ancestor`, DEPLOY_HOLD with `gh variable get`, which is the one
  GitHub call per poll, and `prod` and `last-good` from GHCR. A read that fails keeps the wait
  going and never reads as settled. A failed `prod` read moves no deadline.

It prints a line each time what it waits on changes. Its last line is `verdict=<outcome>
cause=<cause> merge=<M> revision=<R> widened=<why the set was every name, or empty>`.

Like `promote.py`, it reaches GitHub, GHCR, the cluster and syslog only through `forge.py` and
`flux_host.py`, and starts no subprocess itself
(scripts/deploy_tools/tests/test_promote_forge_boundary.py).

Run: uv run python scripts/deploy_tools/promote.py land-wait <sha> --services <tag,...>
     [--also <name,...>]
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
from changed_set import ROOT, ChangedSet, changed_set, suspended_changed, suspended_tags
from artifact_retention import LAST_GOOD_TAG, PROD_TAG
from flux_host import FluxHost

HOLD_VARIABLE = forge.HOLD_VARIABLE
# The `lastAppliedOriginRevision` form that names a commit: the `--revision` that `flux push
# artifact` writes (contract C6). promote.py reads the same form off `prod`'s manifest.
_REVISION = re.compile(r"master@sha1:([0-9a-f]{40})")
# The annotation `flux push artifact --revision` writes on a bundle's manifest. promote.py
# reads `prod`'s commit from it too, through this name.
REVISION_ANNOTATION = "org.opencontainers.image.revision"


class WaitReadError(RuntimeError):
    """A read outside the forge failed: a git command, or a `digests.json` that is not JSON."""


# Both mean "this fact could not be read".
READ_ERRORS = (WaitReadError, forge.ForgeError)

# Seconds between polls. Each poll makes one authenticated GitHub call, so a landing spends at
# most 120 an hour, well inside a token's 5,000.
POLL_S = 30

# The promote timeout as D4.3's chained derivation gives it for a one-tag bundle that
# substitutes, when a rotation changes `cluster-vars` too (d4-ordering.md, *What `promote`
# reads*). The path is `cluster-vars`'s 10m `timeout`, one `chained_timeout.REQUEUE_DEPENDENCY_S`
# hop and the tag's own 10m, both from `manifests_rollout_timeout_default`. One queueing round
# adds the largest changed `timeout`, 10m, because two changed names fit in one round of
# `--concurrent`. Then the OCIRepository interval, 1m in
# `ansible/roles/k8s/flux/templates/sync.yaml.j2`, and the largest `retryInterval`, the 2m
# `scripts/renderer/renderer_lib/bundle.py` writes on each Kustomization. It is the longest wait
# `chained_timeout.promote_timeout` returns for a one-tag bundle (#5155), and
# test_promote_land_wait_deadline.py pins the two together. `chained_timeout` imports this
# module, so this one cannot import `promote_timeout` back to derive the figure from M's bundle.
# `land-wait` gives up at twice it (contract C6). The second half covers `promote.yml`'s
# `render` and `publish` jobs, 10m each, which run before any promotion of M can start.
PROMOTE_TIMEOUT_S = 10 * 60 + 30 + 10 * 60 + 10 * 60 + 60 + 2 * 60
LAND_WAIT_TIMEOUT_S = 2 * PROMOTE_TIMEOUT_S

# How long M's `decide` waits on an earlier commit's verification before it gives up as
# `current`, restated from `verify_promotion.IN_FLIGHT_WAIT_S`, which imports this module
# (test_promote_land_wait_deadline.py pins the two together).
# DECIDED: the deadline moves while that verification is in flight, rather than
# LAND_WAIT_TIMEOUT_S rising by IN_FLIGHT_WAIT_S. The constant would cover the same worst
# case, but every stuck landing would then take about 6.7 hours to give up, where this costs
# only the time a verification is actually in flight (#5164).
IN_FLIGHT_WAIT_S = 350 * 60

# How far back `_published_ancestor` walks M's first-parent history. `publish` pushes every
# green master commit, so the nearest published ancestor is usually M's first parent.
ANCESTOR_WALK_MAX = 50

DIGESTS_FILE = "digests.json"
ANNOTATION_TAG = "deploy-annotation"
HOLD_CAUSE = "promote-held"
TIMEOUT_CAUSE = "promote-timeout"
SUSPENDED_CAUSE = "promote-suspended"


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
        in_flight: the commit `prod` names while its digest differs from `last-good`'s, the
            state `decide` waits on, or None when the two agree or either is absent.
        annotate: writes one syslog line under ANNOTATION_TAG and ignores any failure.
        sleep: waits the given seconds.
        clock: monotonic seconds.
    """

    hold: Callable[[], str | None]
    kustomizations: Callable[[], list[dict]]
    is_ancestor: Callable[[str, str], bool | None]
    digests: Callable[[str], object | None]
    published_ancestor: Callable[[str], str | None]
    in_flight: Callable[[], str | None]
    annotate: Callable[[str], None]
    sleep: Callable[[float], None]
    clock: Callable[[], float]


@dataclass(frozen=True)
class WaitResult:
    """How `land_wait` ended.

    Attributes:
        verdict: `settled`, `deploy-failed`, `gave-up` or `suspended`.
        cause: HOLD_CAUSE, TIMEOUT_CAUSE or SUSPENDED_CAUSE beside the last three, "" beside
            `settled`.
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


def with_dependents(
    names: frozenset[str], depends_on: dict[str, list[str]], suspended: set[str]
) -> frozenset[str]:
    """`names`, plus every Kustomization that depends on one of them, directly or not.

    A suspended dependent is left out, because Flux applies nothing for it.
    """
    found = set(names)
    grew = True
    while grew:
        grew = False
        for name, deps in depends_on.items():
            if name not in found and name not in suspended and found.intersection(deps):
                found.add(name)
                grew = True
    return frozenset(found)


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

    def suspended(self, services: list[str]) -> list[str]:
        """The tags of `services` suspended at M, but not one M changed while suspended.

        A change that raced the gate holds at `decide` (`suspended-changed`), so the wait
        goes on and ends `deploy-failed` there rather than here.
        """
        raced = suspended_changed(self._old, self._new)
        return sorted(suspended_tags(self._new) & set(services) - set(raced))

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
    also: frozenset[str] = frozenset(),
    cap_s: float | None = None,
) -> WaitResult:
    """Poll until a promotion covers `merge`, DEPLOY_HOLD holds, or `timeout_s` passes.

    The first poll reads before any sleep, so a hold set before the merge ends the wait there.

    Args:
        reads: the facts it reads.
        merge: M, the merge commit the landing waits on.
        services: the PR's published tags, named in the settled annotation.
        timeout_s: the seconds after which it gives up.
        poll_s: the seconds between polls.
        also: names to wait on whatever the digests say, each with its live dependents.
        cap_s: the seconds after which it gives up whatever an in-flight verification says,
            or None for no cap. `land.sh` passes what its own deadline leaves.
    """
    started = reads.clock()
    deadline = started + timeout_s
    # The earlier commit last read in flight, and when it was first read so.
    earlier, earlier_since = "", 0.0
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
            if paused := expected.suspended(services):
                return WaitResult(
                    "suspended",
                    SUSPENDED_CAUSE,
                    f"{', '.join(paused)} is suspended at {merge[:8]}, so Flux applies "
                    "nothing for it until a PR removes its flux_suspend",
                    widened=widened,
                )
            names = cs.names | {ROOT}
            if also:
                names |= with_dependents(also, *kustomization_graph(items))
            pending, revision = _pending(reads, merge, items, names)
            if not pending:
                annotate_promotion(reads.annotate, services, revision)
                return WaitResult(
                    "settled",
                    detail=f"{len(names)} Kustomizations applied {revision[:8]}",
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
        now = reads.clock()
        commit, note = _earlier_in_flight(reads, merge)
        if commit and commit != earlier:
            earlier, earlier_since = commit, now
        if commit and now - earlier_since < IN_FLIGHT_WAIT_S:
            deadline = max(deadline, now + timeout_s)
        line += note
        if line != last:
            print(f"  promote.py land-wait: {line}", flush=True)
            last = line
        if now >= deadline or (cap_s is not None and now - started >= cap_s):
            return WaitResult(
                "gave-up",
                TIMEOUT_CAUSE,
                f"no promotion covered {merge[:8]} within {int(now - started)}s. Look first "
                "at the `promote` workflow's failed `publish` jobs: a publish refusal leaves "
                "no sha-<commit> tag to promote",
                widened=widened,
            )
        reads.sleep(poll_s)


def _earlier_in_flight(reads: WaitReads, merge: str) -> tuple[str, str]:
    """The strict ancestor of `merge` whose verification is in flight, or "", and a note.

    A verification of `merge` itself, or of a commit after it, is one the base timeout already
    covers, so it moves no deadline. A read that fails moves none either; the note says so.
    """
    try:
        commit = reads.in_flight()
    except READ_ERRORS as err:
        return "", f"; prod and last-good could not be read: {err}"
    if commit is None or commit == merge or not reads.is_ancestor(commit, merge):
        return "", ""
    return commit, f"; a verification of {commit[:8]}, before this merge, is in flight"


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
        "--also",
        default="",
        help="Kustomizations to wait on whatever the digests say, comma-separated, each "
        "with every Kustomization that depends on it",
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
        help="seconds before it gives up, counted again from each poll that reads an "
        "earlier commit's verification in flight (default: twice the promote timeout)",
    )
