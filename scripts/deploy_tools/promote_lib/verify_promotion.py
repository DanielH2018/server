"""`promote.py verify T` and the rest of contract C2's *Verification*.

After the `tag` job moves `prod` to T, the `verify` job waits on the commit statuses that
notification-controller posts on T, one per Kustomization with the context `flux/<name>`, and
prints `good` or `failed`. The `settle` job applies that verdict: a good one moves `last-good`
to `sha-<T>`, and a failed one writes `<T> failed=<names>` to DEPLOY_HOLD, then resets `prod`
to `last-good`. When `decide` named forward-only tags T changes (its `no_reset` output), a
failed one writes T's SHA alone in place of that value, tags T's digest `held` and leaves
`prod` on T (H1.1b, `forward_only.py`). The plan is
docs/target-architecture/deploy/d2-first-leaf.md, *Publication and verification*, and the
operator's view is docs/flux-deploy-plane.md.

WHAT `verify` EXPECTS. `flux/homelab` plus `flux/<name>` for every name in T's changed set,
which `changed_set.changed_set` computes from T's `digests.json` and `prod`'s, as it does for
`land-wait`. Nothing here derives the set a second time. The Kustomizations and their
`dependsOn` and `suspend` come from T's own bundle, never from the job's `out/`, which is the
render of the commit that triggered the run.

A SUSPENDED TAG IS NOT EXPECTED (D5.1b): it never posts, so expecting it would time out,
hold and reset. `bundle_graph` leaves out every Kustomization of a tag T's `digests.json` marks
`suspended`, and any whose own document sets `spec.suspend: true`.

A DEPENDENT THAT WAS ALREADY FAILING (H1.1i) is excused: reported as `excused <name>` and not
expected. A dependent is a member of the changed set that joined only through its `dependsOn`
chain, its own footprint unchanged. Before `prod` moves, `decide` records the newest
`flux/<name>` status on `prod`'s commit for each name in the changed set (`prior_statuses`)
and hands the record on beside the bundles. `verify` excuses a dependent whose recorded status
is `failure`, so a node outage that fails `longhorn` on every revision does not hold a T that
changes only `cluster-base`. A name whose footprint T changed counts, and so does a dependent
whose record is `success`, `error` or absent. `verify` makes no read of `prod`'s commit itself:
the record is what the cluster showed before T, not after.

WHERE THE BUNDLES COME FROM. `decide` pulls both before `prod` moves and writes them under
`--bundle-dir`, with the record of `prod`'s statuses, so a registry error promotes nothing.
`verify` reads them back and makes no registry call (`verify_handoff.py`).

A SUPERSEDED RUN WRITES NOTHING (D7.2): `prod` naming another digest than T's ends the wait
as `superseded`, which exits 0 (`superseded.py`).

ONLY STATUSES FROM THIS PROMOTION COUNT. T may carry statuses from an earlier promotion of
the same commit, such as the `failure` that held it before an operator cleared the hold and
dispatched `promote` again. `verify` reads T's statuses from `--moved-at`, the time the `tag`
job moved `prod`, less MOVED_AT_SKEW_S, so an older status reads as no status at all.

THE VERDICT. Every expected status `success` is `good`, and `prod` naming another digest is
`superseded`. Any expected `failure` or `error` is
`failed`, naming those. At the timeout it is `failed`, naming every expected name that is not
`success`. In a Longhorn hop another name's failure is provisional (THE STORAGE ROLLOUT). A
missing status, or a `pending` one, keeps the wait going and never reads as success. A status
read that fails is retried at the next poll. The timeout is derived from T's
bundle and its changed set (`chained_timeout.promote_timeout`), and capped at
`chained_timeout.VERIFY_CAP_S`.

THE STORAGE ROLLOUT. While `longhorn` is expected, a failure of any other name is provisional
until `flux/longhorn` has read `success` for a grace, or `longhorn` overruns its own timeout
(`storage_rollout.py`, #5997).

THE CONTROLLER ROLLOUT. While `flux` is expected, a failure of `flux` or `soak` is provisional:
a later `success` on the same name ends it, and none inside a fixed grace turns it final
(`controller_rollout.py`, #6013).

THE OTHER TWO HALVES `decide` calls:
  - `wait_settled`, the wait on a verification in flight (the DECIDED marker below);
  - `revertible` and `footprints_match`, the revert rule, which `promote.REVERT_RULE` keeps off.

Like `promote.py`, this module reaches GitHub only through `forge.py`
(scripts/deploy_tools/tests/test_promote_forge_boundary.py).

Run: uv run python scripts/deploy_tools/promote.py verify <sha> --bundle-dir <dir> --prior <sha>
"""

import argparse
import json
import re
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import yaml

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
# `forge`, `changed_set` and `chained_timeout` import bare, from scripts/deploy_tools.
_sys.path.insert(1, str(_Path(__file__).resolve().parents[1]))
from lib import yaml_fast
from lib.cli_help import error
from lib.exit_codes import FAILED, OK, USAGE_ERROR

import forge
from artifact_retention import HELD_TAG, LAST_GOOD_TAG, PROD_TAG
from changed_set import ROOT, changed_set, footprints, suspended_tags
from chained_timeout import promote_timeout
from deploy_tools.kustomizations import kustomization_graph
from deploy_tools.promote_lib import controller_rollout, storage_rollout, superseded
from deploy_tools.promote_lib.verify_handoff import read_handoff, read_recorded

# Re-exported, as `verify.write_handoff`, for `promote.py`'s `hand_off`.
from deploy_tools.promote_lib.verify_handoff import write_handoff as write_handoff
from renderer.renderer_lib.ordering import split_kind

DIGESTS_FILE = "digests.json"
KUSTOMIZATION_API = "kustomize.toolkit.fluxcd.io/v1"
STATUS_PREFIX = "flux/"
FAILED_STATES = frozenset({"failure", "error"})
_SHA = re.compile(r"[0-9a-f]{40}")

# How far before `--moved-at` a status on T still counts, for clock skew between the runner
# that stamped the move and GitHub. A status left by an earlier promotion of T is older than
# that by a whole failed run: a verify timeout, a settle, an operator's clear and a new render.
MOVED_AT_SKEW_S = 60

# Seconds between two status reads. Each is one GITHUB_TOKEN call, so a 15-minute wait spends
# 30 of the token's 1,000 an hour.
POLL_S = 30


# DECIDED: a verification in flight is guarded by two checks rather than by joining `verify`
# and `settle` to the `promote` concurrency group. GitHub keeps one pending job per group and
# a newer one cancels it, so a pending `settle` could be evicted with its hold unwritten.
# Unguarded, a second run could move `prod` past a T that `verify` still waits on, Flux would
# never post T's statuses, and the timeout would write a false hold and reset `prod` under the
# second run's verified commit. So `decide` waits while `prod` differs from `last-good` (unless
# `promote_lib/supersede.py` lets T supersede the run, D7), which
# it does only between a run's `tag` and its `settle`, at two HEADs a poll. And `tag` moves
# `prod` only while it still names the digest `decide` read, which closes the gap between one
# run's `decide` and an earlier run's `tag` (promote.yml, the `Move prod` step). A run that never settles stalls promotion as `current`, which D2.6d's
# `PromotionStalled` alert pages once it lands. The long form is docs/flux-deploy-plane.md,
# *Verification*.
#
# Longer than the `verify` job's timeout-minutes plus `settle`'s, with ten minutes for runner
# queueing, so the wait outlasts any run still verifying
# (ansible/tests/repo/test_promote_workflow_verify.py). `verify`'s limit sits just above
# `chained_timeout.VERIFY_CAP_S`, so this is 335 + 5 + 10 minutes, and the `promote` job that holds it fits
# inside GitHub's 6-hour job limit with 10 minutes to spare.
IN_FLIGHT_WAIT_S = 350 * 60
IN_FLIGHT_POLL_S = 30

# `<T> failed=<names>`, the one hold form the revert rule may pass. `settle` writes it.
_FAILED_HOLD = re.compile(r"([0-9a-f]{40}) failed=([a-z0-9-]+(?:,[a-z0-9-]+)*)")
# A soak verdict covers every service in the changed set, so no footprint shows it reverted.
SOAK = "soak"


# -- the bundles -----------------------------------------------------------------------------


def bundle_digests(files: dict[str, bytes] | None) -> object | None:
    """A bundle's parsed `digests.json`, or None when the bundle has none or it is not JSON."""
    raw = (files or {}).get(DIGESTS_FILE)
    try:
        return None if raw is None else json.loads(raw)
    except ValueError:
        return None


def flux_kustomizations(files: dict[str, bytes]) -> list[dict]:
    """Every Flux Kustomization document under the bundle's `flux/`.

    The kustomize `flux/kustomization.yaml` is not one: it is another apiVersion and kind.
    """
    found = []
    for name in sorted(files):
        if not (name.startswith("flux/") and name.endswith(".yaml")):
            continue
        for doc in yaml_fast.safe_load_all(files[name]):
            if (
                isinstance(doc, dict)
                and doc.get("apiVersion") == KUSTOMIZATION_API
                and doc.get("kind") == "Kustomization"
            ):
                found.append(doc)
    return found


def bundle_graph(
    target: dict[str, bytes], docs: list[dict]
) -> tuple[dict[str, list[str]], set[str]]:
    """T's `dependsOn` per Kustomization, and the Kustomizations suspended in T.

    A Kustomization is suspended when its document sets `spec.suspend: true` or when it
    belongs to a tag whose object in T's `digests.json` reads `suspended: true`.
    """
    depends_on, suspended = kustomization_graph(docs)
    tags = suspended_tags(bundle_digests(target))
    suspended |= {name for name in depends_on if split_kind(name)[0] in tags}
    return depends_on, suspended


@dataclass(frozen=True)
class Expected:
    """What `verify` waits on.

    Attributes:
        names: the Kustomizations whose `flux/<name>` status must read `success`, `ROOT`
            included.
        dependents: the members of the changed set that joined through `dependsOn`. Each is
            excused rather than expected when its recorded status on `prod`'s commit read
            `failure`.
        widened: why every name counts as changed, or "" when the digests decided.
        timeout_s: how long to wait.
        storage_grace_s: `storage_rollout.grace_s`, 0 when `longhorn` is not expected.
        storage_rollout_s: `storage_rollout.rollout_s`, 0 when `longhorn` is not expected.
    """

    names: frozenset[str]
    dependents: frozenset[str]
    widened: str
    timeout_s: int
    storage_grace_s: int = 0
    storage_rollout_s: int = 0


def expected_set(target: dict[str, bytes], prod: dict[str, bytes] | None) -> Expected:
    """T's changed set against `prod`, from the two bundles, plus `ROOT`.

    A bundle with no `digests.json`, published before D2.6b, counts as every name changed,
    and so does a first run with no `prod`.

    Raises:
        ValueError: a Kustomization's duration is unreadable, or its `dependsOn` names one
            the bundle does not hold or forms a cycle (`promote_timeout`).
        yaml.YAMLError: a file under `flux/` is not YAML.
    """
    docs = flux_kustomizations(target)
    depends_on, suspended = bundle_graph(target, docs)
    old = bundle_digests(prod) if prod is not None else None
    found = changed_set(bundle_digests(target), old, depends_on, suspended)
    grace = storage_rollout.grace_s(docs, found.names)
    return Expected(
        names=found.names | {ROOT},
        dependents=found.names - found.direct,
        widened=found.widened,
        timeout_s=promote_timeout(docs, found.names),
        storage_grace_s=grace,
        storage_rollout_s=storage_rollout.rollout_s(docs, grace),
    )


def prior_statuses(
    target: dict[str, bytes],
    prod: dict[str, bytes],
    read: Callable[[], dict[str, str]],
) -> dict[str, str]:
    """`decide`'s record: the newest status on `prod`'s commit for each name in the changed set.

    `read` returns the newest status per context on `prod`'s commit. It is called once, and
    only when the changed set holds a dependent, because only a dependent can be excused. A
    name with no status is absent from the record. A bundle whose Kustomizations do not parse
    records nothing: `verify` fails that T on the same bundle before it reads the record.

    Raises:
        Whatever `read` raises, so a failed read is `decide`'s `error` and moves nothing.
    """
    try:
        depends_on, suspended = bundle_graph(target, flux_kustomizations(target))
    except yaml.YAMLError:
        return {}
    found = changed_set(
        bundle_digests(target), bundle_digests(prod), depends_on, suspended
    )
    if not found.names - found.direct:
        return {}
    states = read()
    return {
        name: states[STATUS_PREFIX + name]
        for name in sorted(found.names)
        if STATUS_PREFIX + name in states
    }


# -- the wait --------------------------------------------------------------------------------


@dataclass(frozen=True)
class StatusReads:
    """The reads `verify` makes.

    Attributes:
        statuses: the newest status per context on a commit, raising `forge.ForgeError`.
            With a time, it leaves out every status updated before it.
        sleep: waits the given seconds.
        clock: monotonic seconds.
        prod: `prod`'s digest, None when the tag is absent, raising `forge.ForgeError`.
            None in place of the callable reads no digest, so no run is ever superseded.
    """

    statuses: Callable[[str, datetime | None], dict[str, str]]
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    prod: Callable[[], str | None] | None = None


@dataclass(frozen=True)
class Verdict:
    """How `verify` ended.

    Attributes:
        verdict: `good`, `failed` or `superseded`.
        failed: the names `settle` writes into `<T> failed=<names>`, sorted.
        excused: the dependents left out because their recorded status on `prod`'s commit
            read `failure`.
    """

    verdict: str
    failed: tuple[str, ...] = ()
    excused: tuple[str, ...] = ()


def already_failing(
    dependents: frozenset[str], recorded: Mapping[str, str]
) -> frozenset[str]:
    """The dependents whose status `decide` recorded on `prod`'s commit read `failure`.

    Only `failure` excuses. An `error`, a `success` or no record keeps the dependent expected.
    """
    return frozenset(name for name in dependents if recorded.get(name) == "failure")


def verify(
    reads: StatusReads,
    target: str,
    prior: str,
    expected: Expected,
    since: datetime | None = None,
    poll_s: int = POLL_S,
    recorded: Mapping[str, str] | None = None,
    digest: str = "",
) -> Verdict:
    """Wait on T's statuses for the expected names until one fails, all pass or time runs out.

    Args:
        reads: the status reads, of T alone.
        target: T, the commit `decide` printed. Never the commit that triggered the run.
        prior: `prod`'s commit before the move, or "" on the first run. Printed only.
        expected: what to wait on.
        since: the earliest update a status on T may carry and still count, so a status
            from an earlier promotion of T reads as none. None counts every status.
        poll_s: the seconds between reads.
        recorded: the statuses `decide` recorded on `prod`'s commit before the move, by
            name. None is no record, which excuses nothing.
        digest: T's digest, which `prod` must still name for any verdict but
            `superseded`. Empty reads no digest.
    """
    excused = already_failing(expected.dependents, recorded or {})
    names = sorted(expected.names - excused)
    for name in sorted(excused):
        print(
            f"  verify: excused {name}: {STATUS_PREFIX}{name} read failure on prod's "
            f"commit {prior[:8]} before the move, and T leaves its footprint unchanged"
        )
    print(
        f"  verify: expecting {', '.join(STATUS_PREFIX + n for n in names)} on "
        f"{target[:8]} within {expected.timeout_s}s"
        + (f" (every name: {expected.widened})" if expected.widened else ""),
        flush=True,
    )
    started = reads.clock()
    states: dict[str, str] = {}
    last = ""
    controller = controller_rollout.Window()
    window = storage_rollout.Window(
        names, expected.storage_grace_s, expected.storage_rollout_s
    )

    def moved_past() -> Verdict | None:
        if superseded.moved_past(reads.prod, digest) is None:
            return None
        return Verdict("superseded", excused=tuple(sorted(excused)))

    while True:
        if moved := moved_past():
            return moved
        try:
            states = reads.statuses(target, since)
            line = ", ".join(
                f"{n}={states.get(STATUS_PREFIX + n, 'none')}" for n in names
            )
        except forge.ForgeError as err:
            line = f"the statuses could not be read: {err}"
        failed = [n for n in names if states.get(STATUS_PREFIX + n) in FAILED_STATES]
        rolling = controller.provisional(names, failed, reads.clock())
        final = [n for n in failed if n not in rolling]
        if final and not window.provisional(final, states, reads.clock()):
            return moved_past() or Verdict(
                "failed", tuple(failed), tuple(sorted(excused))
            )
        if failed:
            line += window.note() if final else controller.note()
        if all(states.get(STATUS_PREFIX + n) == "success" for n in names):
            return Verdict("good", excused=tuple(sorted(excused)))
        if line != last:
            print(f"  verify: {line}", flush=True)
            last = line
        if reads.clock() - started >= expected.timeout_s:
            missing = [n for n in names if states.get(STATUS_PREFIX + n) != "success"]
            return moved_past() or Verdict(
                "failed", tuple(missing), tuple(sorted(excused))
            )
        reads.sleep(poll_s)


def report(target: str, verdict: Verdict, github_output: str | None) -> int:
    """Print the last line, append the step outputs, and return the exit code.

    A failed verdict exits 1 so the run reads red, after its outputs are written for `settle`.
    A superseded one exits 0: the later run's verification owns the outcome.
    """
    failed = ",".join(verdict.failed)
    print(f"target={target} verdict={verdict.verdict} failed={failed}")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as out:
            out.write(f"verdict={verdict.verdict}\nfailed={failed}\n")
    return OK if verdict.verdict in ("good", "superseded") else FAILED


def add_parser(commands) -> None:
    """Add the `verify` subcommand to `promote.py`'s subparsers."""
    cmd = commands.add_parser(
        "verify",
        help="wait on T's commit statuses for its changed set (the verify job)",
    )
    cmd.add_argument("target", help="T, the commit decide printed")
    cmd.add_argument(
        "--bundle-dir",
        type=Path,
        required=True,
        help="where decide wrote prod's and T's bundles",
    )
    cmd.add_argument(
        "--prior",
        default="",
        help="prod's commit before the move; empty on a first run",
    )
    cmd.add_argument(
        "--digest",
        default="",
        help="T's digest; prod naming another one ends the wait as superseded",
    )
    cmd.add_argument(
        "--moved-at",
        required=True,
        help="when the tag job moved prod, as ISO 8601 UTC; older statuses on T are ignored",
    )


def run(f: forge.Forge, args: argparse.Namespace, github_output: str | None) -> int:
    """`promote.py verify`: read the handoff, wait, and report."""
    if not _SHA.fullmatch(args.target) or (
        args.prior and not _SHA.fullmatch(args.prior)
    ):
        error(f"verify takes 40-hex commits, not {args.target!r} and {args.prior!r}")
        return USAGE_ERROR
    try:
        moved_at = datetime.fromisoformat(args.moved_at)
    except ValueError:
        moved_at = None
    if moved_at is None or moved_at.tzinfo is None:
        error(
            f"verify: --moved-at {args.moved_at!r} is not an ISO 8601 time with a zone"
        )
        return USAGE_ERROR
    try:
        prod, target = read_handoff(args.bundle_dir)
        expected = expected_set(target, prod)
        recorded = read_recorded(args.bundle_dir)
    except (OSError, ValueError, yaml.YAMLError) as err:
        error(f"verify: the bundles decide handed on: {err}")
        return FAILED
    since = moved_at - timedelta(seconds=MOVED_AT_SKEW_S)
    if args.digest and not re.fullmatch(r"sha256:[0-9a-f]{64}", args.digest):
        error(f"verify: --digest {args.digest!r} is not sha256:<64 hex>")
        return USAGE_ERROR
    reads = StatusReads(f.github.statuses, prod=lambda: f.registry.digest(PROD_TAG))
    verdict = verify(
        reads,
        args.target,
        args.prior,
        expected,
        since,
        recorded=recorded,
        digest=args.digest,
    )
    return report(args.target, verdict, github_output)


# -- what `decide` calls ---------------------------------------------------------------------


def revertible(hold: str, enabled: bool) -> tuple[str, frozenset[str]] | None:
    """The held commit and failed names of a hold the revert rule may pass, else None.

    None while the rule is off, for a hold in any form but `<T> failed=<names>`, and for a
    `failed=` hold that names SOAK.
    """
    match = _FAILED_HOLD.fullmatch(hold) if enabled else None
    if match is None:
        return None
    names = frozenset(match.group(2).split(","))
    return None if SOAK in names else (match.group(1), names)


def footprints_match(
    candidate: dict[str, bytes] | None,
    last_good: dict[str, bytes] | None,
    names: frozenset[str],
) -> bool:
    """Whether each name's `footprint` in the candidate's bundle equals `last-good`'s.

    A bundle without a readable `digests.json` never matches.
    """
    ours = footprints(bundle_digests(candidate))
    theirs = footprints(bundle_digests(last_good))
    if ours is None or theirs is None:
        return False
    return all(ours.get(name) == theirs.get(name) for name in names)


@dataclass(frozen=True)
class Settled:
    """What `wait_settled` read once no verification was in flight, or why the run ends.

    Attributes:
        stop: `(verdict, reason)` when the run ends here, else None.
        held: the revertible hold as `(commit, names)`, or None when DEPLOY_HOLD is `none`.
        prod: `prod`'s digest, or None when the tag is absent.
        last_good: `last-good`'s digest, or None when the tag is absent.
        hold: the DEPLOY_HOLD value read, which `tag` compares before it moves `prod`.
        in_flight: `prod`'s run is still verifying, and `supersedable` let T take it over.
    """

    stop: tuple[str, str] | None = None
    held: tuple[str, frozenset[str]] | None = None
    prod: str | None = None
    last_good: str | None = None
    hold: str = "none"
    in_flight: bool = False


# DECIDED: a hold without a reset (H1.1b) leaves `prod` on the failed T while `last-good` stays
# behind, and that difference outlives the operator's clear of DEPLOY_HOLD. So `settle` tags
# T's digest `held` when it leaves `prod` there, and a `prod` that names the `held` digest is
# settled, not in flight. `prod` never returns to that digest once it moves on: it moves only
# forward, or back to `last-good`, which T never became. Without the tag, every run after the
# clear would wait IN_FLIGHT_WAIT_S and end `current`.
def wait_settled(
    hold: Callable[[], str | None],
    digest: Callable[[str], str | None],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    revert_rule: bool,
    supersedable: Callable[[str], bool] | None = None,
) -> Settled:
    """Read DEPLOY_HOLD, then wait while `prod` and `last-good` name different digests.

    Each poll re-reads the hold, so a `failed=` hold that a `settle` writes ends the wait. A
    `prod` that names the `held` tag's digest ends it too: a hold without a reset settled it.

    Args:
        hold: DEPLOY_HOLD's value, or None when the variable is missing.
        digest: the digest a tag names, or None when the tag is absent.
        sleep: waits the given seconds.
        clock: monotonic seconds.
        revert_rule: whether a `failed=` hold may be passed.
        supersedable: whether T may take over the run that `prod`'s digest names (D7). When
            it answers True under a `none` hold, the wait ends at once with `in_flight`.

    Raises:
        Whatever `hold` and `digest` raise.
    """
    started = clock()
    while True:
        value = hold()
        if value is None:
            return Settled(("error", f"{forge.HOLD_VARIABLE} does not exist"))
        value = value.strip()
        if not value:
            return Settled(("error", f"{forge.HOLD_VARIABLE} is empty"))
        held = None if value == "none" else revertible(value, revert_rule)
        if value != "none" and held is None:
            return Settled(("held", f"{forge.HOLD_VARIABLE}={value}"))
        prod, last_good = digest(PROD_TAG), digest(LAST_GOOD_TAG)
        if last_good is None or prod is None or prod == last_good:
            return Settled(None, held, prod, last_good, value)
        if digest(HELD_TAG) == prod:
            return Settled(None, held, prod, last_good, value)
        if supersedable is not None and value == "none" and supersedable(prod):
            return Settled(None, held, prod, last_good, value, in_flight=True)
        waited = int(clock() - started)
        if waited >= IN_FLIGHT_WAIT_S:
            return Settled(
                (
                    "current",
                    f"prod {prod} is still not last-good {last_good} after {waited}s: "
                    "a verification started and never settled",
                )
            )
        print(
            f"promote.py: prod {prod} is not last-good {last_good}, so a verification is "
            f"in flight; waiting {IN_FLIGHT_POLL_S}s",
            file=sys.stderr,
        )
        sleep(IN_FLIGHT_POLL_S)
