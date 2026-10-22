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
                   from it. When the set holds `soak`, R also carries a `flux/soak` `success`
                   (D5.3b3). One `deploy-annotation` line records it.
    deploy-failed  DEPLOY_HOLD holds a value other than `none`. Cause `promote-held`. Or
                   R carries a `flux/soak` `failure` or `error`, read before `verify` writes
                   the hold. Cause `soak-failed`.
    gave-up        LAND_WAIT_TIMEOUT_S passed, or the later deadline below. Cause
                   `promote-timeout`.
    suspended      Every one of `--services` is `suspended` in M's `digests.json`, so Flux
                   applies nothing for them until a PR resumes them (D5.1d), and no `--also`
                   name is left: the first poll that reads M's file and a `none` DEPLOY_HOLD.
                   The suspend PR ends this way. Cause `promote-suspended`. Some suspended,
                   the wait settles on the rest; one M changed while suspended at both ends
                   is left to `decide`'s hold.
- While a verification of an earlier commit P is in flight, M's `decide` waits on it rather
  than promoting, for up to IN_FLIGHT_WAIT_S. So each poll that reads `prod` differing from
  `last-good` at a strict ancestor of M moves the deadline to LAND_WAIT_TIMEOUT_S past that
  poll. It stops moving IN_FLIGHT_WAIT_S after P was first read in flight, because `decide`
  gives up there too (#5164).
- Inside the weekly reboot's no-promotion window `decide` defers (D5.2b2), so each poll inside
  it moves the deadline to LAND_WAIT_TIMEOUT_S past the window's end and prints that end. The
  window is the widest any T has, VERIFY_CAP_S, since a later T than M may promote M.
  The first such poll also moves `land.sh`'s cap by the window's remaining length, up to
  `window_grace_s`, so a lander whose unit leaves room for the window waits the deferral out
  rather than giving up on a promotion nothing has failed (#5457).
- It reads the Kustomizations with the read-only `kubectl`, ancestry with
  `git merge-base --is-ancestor`, DEPLOY_HOLD with `gh variable get`, R's `flux/soak` status
  once the Kustomizations have settled, and `prod` and `last-good` from GHCR. A read that fails keeps the wait
  going and never reads as settled. A failed `prod` read moves no deadline.

It prints a line each time what it waits on changes. Its last line is `verdict=<outcome>
cause=<cause> merge=<M> revision=<R> widened=<why the set was every name, or empty>`.

Like `promote.py`, it reaches GitHub, GHCR, the cluster and syslog only through `forge.py` and
`flux_host.py`, and starts no subprocess itself
(scripts/deploy_tools/tests/test_promote_forge_boundary.py).

Run: uv run python scripts/deploy_tools/promote.py land-wait <sha> --services <tag,...>
     [--also <name,...>]
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
# `forge`, `changed_set` and `flux_host` import bare, from scripts/deploy_tools.
_sys.path.insert(1, str(_Path(__file__).resolve().parents[1]))
from lib.exit_codes import FAILED, LAND_GAVE_UP, OK

import forge
from chained_timeout import OCI_INTERVAL_S, seconds
from renderer.renderer_lib.ordering import split_kind
from changed_set import (
    ROOT,
    SOAK,
    ChangedSet,
    changed_set,
    suspended_changed,
    suspended_tags,
)
from deploy_tools.kustomizations import (
    is_ready,
    kustomization_graph,
    kustomization_name as _name,
    origin_commit,
    with_dependents,
)
from deploy_tools.promote_lib import reboot_window

HOLD_VARIABLE = forge.HOLD_VARIABLE
# The annotation `flux push artifact --revision` writes on a bundle's manifest. promote.py
# reads `prod`'s commit from it too, through this name.
REVISION_ANNOTATION = "org.opencontainers.image.revision"


class WaitReadError(RuntimeError):
    """A read outside the forge failed: a git command, or a `digests.json` that is not JSON."""


# Both mean "this fact could not be read".
READ_ERRORS = (WaitReadError, forge.ForgeError)

# Seconds between polls. A poll makes one authenticated GitHub call, two while it awaits the
# soak's verdict: at most 240 an hour, well inside a token's 5,000.
POLL_S = 30

# The promote timeout as D4.3's chained derivation gives it for a one-tag bundle that
# substitutes, when a rotation changes `cluster-vars` too (d4-ordering.md, *What `promote`
# reads*). `_TAGS_S` is the tags' share: `cluster-vars`'s 10m, one requeue hop and the tag's
# 10m on the path, one 10m queueing round, the 1m OCIRepository interval and the 2m
# `retryInterval`. `verify` expects `soak` too (D5.3b3), whose `timeout` the renderer sets to
# `_TAGS_S` plus W (below) and two 1m margins; that term is the longest, so the total adds the
# interval and the `retryInterval` again. test_promote_land_wait_deadline.py pins this to
# `chained_timeout.promote_timeout` (#5155). `land-wait` gives up at twice it
# (contract C6): the second half covers `promote.yml`'s `render` and `publish`, 10m each.
# The soak's chain counts every Kustomization in the bundle, so `land_wait` raises the deadline
# from the live `soak` Kustomization's `timeout` too (`soak_deadline_s`).
_TAGS_S = 10 * 60 + 30 + 10 * 60 + 10 * 60 + 60 + 2 * 60
# W is 4m. `IngressProbeFailedCritical` pages at `for: 2m` with no `service` label, so since M4's
# PR 5 dropped its `shadow` it counts for every tag: W = max(stabilise, 2m) + 2m scrape-and-eval.
PROMOTE_TIMEOUT_S = _TAGS_S + 4 * 60 + 2 * 60 + 60 + 2 * 60
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
SOAK_CAUSE = "soak-failed"
SOAK_CONTEXT = "flux/soak"
SOAK_FAILED_STATES = frozenset({"failure", "error"})
# Prints the services a failed soak named; the renderer labels the soak Job's pod `app: soak`.
SOAK_READ = (
    "kubectl -n flux-system get pods -l app=soak -o "
    "jsonpath='{.items[*].status.containerStatuses[0].state.terminated.message}'"
)


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
        soak: the state of the newest `flux/soak` status on commit R, or None when R has
            none.
        reboot_window: `(seconds left, end)` while inside the reboot's no-promotion window.
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
    soak: Callable[[str], str | None]
    reboot_window: Callable[[], tuple[int, datetime] | None]
    annotate: Callable[[str], None]
    sleep: Callable[[float], None]
    clock: Callable[[], float]


@dataclass(frozen=True)
class WaitResult:
    """How `land_wait` ended.

    Attributes:
        verdict: `settled`, `deploy-failed`, `gave-up` or `suspended`.
        cause: HOLD_CAUSE or SOAK_CAUSE beside `deploy-failed`, TIMEOUT_CAUSE or
            SUSPENDED_CAUSE beside the last two, "" beside `settled`.
        detail: why, in one sentence.
        revision: R, the commit `homelab` applied, for `settled`.
        widened: why the set was every name, or "" when the digests decided it.
        suspended: the PR's tags suspended at M, which the wait left out.
    """

    verdict: str
    cause: str = ""
    detail: str = ""
    revision: str = ""
    widened: str = ""
    suspended: tuple[str, ...] = ()


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
        """The tags of `services` suspended at M, less any M changed while suspended."""
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
    reads: WaitReads, merge: str, items: list[dict], expected: set[str] | frozenset[str]
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
    window_grace_s: int = 0,
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
        window_grace_s: the most the reboot's no-promotion window may add to `cap_s`. The
            first poll inside the window adds the window's remaining seconds, up to this.
    """
    started = reads.clock()
    deadline = started + timeout_s
    cap_end = None if cap_s is None else started + cap_s
    window_seen = False
    # The earlier commit last read in flight, and when it was first read so.
    earlier, earlier_since = "", 0.0
    expected = _Expected(reads, merge)
    last = widened = ""
    paused, all_paused = [], False
    while True:
        try:
            items = reads.kustomizations()
        except READ_ERRORS as err:
            items, line = None, f"the Kustomizations could not be read: {err}"
        if items is not None:
            cs = expected.changes(items)
            widened, paused = cs.widened, expected.suspended(services)
            # Every tag the wait covers is suspended, so nothing of M is left to wait on.
            all_paused = bool(paused) and set(paused) == set(services) and not also
            # A suspended tag's Kustomizations never apply M, so the rest are waited on.
            # `soak` is never waited on Ready. It reads Ready only once its Job has judged,
            # so the wait reads R's `flux/soak` verdict instead (D5, *The soak as a Job*).
            names = {
                n for n in cs.names if split_kind(n)[0] not in paused and n != SOAK
            } | {ROOT}
            if also:
                names |= with_dependents(also, *kustomization_graph(items))
            pending, revision = _pending(reads, merge, items, names)
            if SOAK in cs.names:
                deadline = max(deadline, started + soak_deadline_s(items))
            if not pending and not all_paused and SOAK in cs.names:
                soak = _soak_state(reads, revision)
                if soak in SOAK_FAILED_STATES:
                    return WaitResult(
                        "deploy-failed",
                        SOAK_CAUSE,
                        f"{SOAK_CONTEXT} reads {soak} on {revision[:8]}. The soak Job "
                        f"names the services it failed: `{SOAK_READ}`",
                        revision=revision,
                        widened=widened,
                    )
                if soak != "success":
                    pending = [f"{SOAK_CONTEXT} on {revision[:8]} ({soak})"]
            if not pending and not all_paused:
                annotate_promotion(reads.annotate, services, revision)
                return WaitResult(
                    "settled",
                    detail=f"{len(names)} Kustomizations applied {revision[:8]}",
                    revision=revision,
                    widened=widened,
                    suspended=tuple(paused),
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
        if all_paused:
            return WaitResult(
                "suspended",
                SUSPENDED_CAUSE,
                f"{', '.join(paused)} is suspended at {merge[:8]}, so Flux applies "
                "nothing for it until a PR removes its flux_suspend",
                widened=widened,
                suspended=tuple(paused),
            )
        now = reads.clock()
        commit, note = _earlier_in_flight(reads, merge)
        if commit and commit != earlier:
            earlier, earlier_since = commit, now
        if commit and now - earlier_since < IN_FLIGHT_WAIT_S:
            deadline = max(deadline, now + timeout_s)
        left, window_note = _reboot_window(reads)
        if left is not None:
            deadline = max(deadline, now + left + timeout_s)
            # DECIDED: once per wait. A cap is hours long and windows are a week apart, so a
            # wait never meets a second one; adding on every poll would count the window again
            # each time.
            if cap_end is not None and not window_seen:
                cap_end += min(left, window_grace_s)
            window_seen = True
        line += note + window_note
        if line != last:
            print(f"  promote.py land-wait: {line}", flush=True)
            last = line
        if now >= deadline or (cap_end is not None and now >= cap_end):
            return WaitResult(
                "gave-up",
                TIMEOUT_CAUSE,
                f"no promotion covered {merge[:8]} within {int(now - started)}s. Look first "
                "at the `promote` workflow's failed `publish` jobs: a publish refusal leaves "
                "no sha-<commit> tag to promote",
                widened=widened,
            )
        reads.sleep(poll_s)


def soak_deadline_s(items: list[dict]) -> int:
    """Twice `verify`'s wait on `soak`: its listed `timeout`, the interval and `retryInterval`.

    0 when `soak` is not listed or its `timeout` is unreadable, so the constant stands.
    """
    spec = next((i.get("spec") or {} for i in items if _name(i) == SOAK), {})
    try:
        return 2 * (seconds(spec.get("timeout", "")) + OCI_INTERVAL_S + 2 * 60)
    except ValueError:
        return 0


def _soak_state(reads: WaitReads, revision: str) -> str:
    """R's `flux/soak` state, `no status` when it has none, or why the read failed."""
    try:
        return reads.soak(revision) or "no status"
    except READ_ERRORS as err:
        return f"unreadable: {err}"


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


def _reboot_window(reads: WaitReads) -> tuple[int | None, str]:
    """Seconds until the reboot's no-promotion window ends, None outside it, and a note."""
    try:
        found = reads.reboot_window()
    except (*READ_ERRORS, reboot_window.ReadError) as err:
        return None, f"; the reboot schedule could not be read: {err}"
    if found is None:
        return None, ""
    return found[
        0
    ], f"; promotions wait for the reboot window's end, {found[1]:%a %H:%M} UTC"


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
