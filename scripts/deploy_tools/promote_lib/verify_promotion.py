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
render of the commit that triggered the run. A dependent whose status on `prod`'s commit
already read `failure` is reported and not expected, so a failure that predates T never holds.

WHERE THE BUNDLES COME FROM. `decide` pulls both before `prod` moves and writes them under
`--bundle-dir` (`write_handoff`), so a registry error promotes nothing. `verify` reads them
back (`read_handoff`) and makes no registry call.

ONLY STATUSES FROM THIS PROMOTION COUNT. T may carry statuses from an earlier promotion of
the same commit, such as the `failure` that held it before an operator cleared the hold and
dispatched `promote` again. `verify` reads T's statuses from `--moved-at`, the time the `tag`
job moved `prod`, less MOVED_AT_SKEW_S, so an older status reads as no status at all.

THE VERDICT. Every expected status `success` is `good`. Any expected `failure` or `error` is
`failed`, naming those. At the timeout it is `failed`, naming every expected name that is not
`success`. A missing status, or a `pending` one, keeps the wait going and never reads as
success. A status read that fails is retried at the next poll. The timeout is derived from T's
bundle and its changed set (`chained_timeout.promote_timeout`), and capped at
`chained_timeout.VERIFY_CAP_S`.

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
from collections.abc import Callable
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
from changed_set import ROOT, changed_set, footprints
from chained_timeout import promote_timeout
from deploy_tools.promote_lib.wait import kustomization_graph

DIGESTS_FILE = "digests.json"
# The files `verify` reads from each bundle. Nothing else is handed on.
_HANDOFF_FILE = re.compile(r"digests\.json|flux/[a-z0-9][a-z0-9.-]*\.yaml")
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
# second run's verified commit. So `decide` waits while `prod` differs from `last-good`, which
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


def write_handoff(
    directory: Path, prod: dict[str, bytes] | None, target: dict[str, bytes]
) -> None:
    """Write the files `verify` reads from `prod`'s bundle and T's under `directory`.

    `prod` is None on the first run, which has no `prod`, and leaves `directory/prod` absent.

    Raises:
        OSError: a write failed.
    """
    for side, files in (("prod", prod), ("target", target)):
        for name, raw in (files or {}).items():
            if _HANDOFF_FILE.fullmatch(name):
                path = directory / side / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
    (directory / "target").mkdir(parents=True, exist_ok=True)


def read_handoff(
    directory: Path,
) -> tuple[dict[str, bytes] | None, dict[str, bytes]]:
    """`prod`'s files, or None when `decide` had no `prod`, and T's files.

    Raises:
        ValueError: `directory/target` does not exist, so `decide` wrote no handoff.
    """

    def files(side: Path) -> dict[str, bytes]:
        return {
            path.relative_to(side).as_posix(): path.read_bytes()
            for path in side.rglob("*")
            if path.is_file()
        }

    if not (directory / "target").is_dir():
        raise ValueError(f"{directory} holds no target/ that decide wrote")
    prod = directory / "prod"
    return (files(prod) if prod.is_dir() else None), files(directory / "target")


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


@dataclass(frozen=True)
class Expected:
    """What `verify` waits on.

    Attributes:
        names: the Kustomizations whose `flux/<name>` status must read `success`, `ROOT`
            included.
        dependents: the members of the changed set that joined through `dependsOn`. Each is
            reported rather than expected when it already failed on `prod`'s commit.
        widened: why every name counts as changed, or "" when the digests decided.
        timeout_s: how long to wait.
    """

    names: frozenset[str]
    dependents: frozenset[str]
    widened: str
    timeout_s: int


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
    depends_on, suspended = kustomization_graph(docs)
    old = bundle_digests(prod) if prod is not None else None
    found = changed_set(bundle_digests(target), old, depends_on, suspended)
    return Expected(
        names=found.names | {ROOT},
        dependents=found.names - found.direct,
        widened=found.widened,
        timeout_s=promote_timeout(docs, found.names),
    )


# -- the wait --------------------------------------------------------------------------------


@dataclass(frozen=True)
class StatusReads:
    """The reads `verify` makes.

    Attributes:
        statuses: the newest status per context on a commit, raising `forge.ForgeError`.
            With a time, it leaves out every status updated before it.
        sleep: waits the given seconds.
        clock: monotonic seconds.
    """

    statuses: Callable[[str, datetime | None], dict[str, str]]
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic


@dataclass(frozen=True)
class Verdict:
    """How `verify` ended.

    Attributes:
        verdict: `good` or `failed`.
        failed: the names `settle` writes into `<T> failed=<names>`, sorted.
        reported: the dependents left out because they already failed on `prod`'s commit.
    """

    verdict: str
    failed: tuple[str, ...] = ()
    reported: tuple[str, ...] = ()


def already_failing(
    reads: StatusReads, prior: str, dependents: frozenset[str]
) -> frozenset[str]:
    """The dependents whose status on `prod`'s commit `prior` already read `failure`.

    One status read, made only when there is a dependent to check. A failed read reports
    none, so each dependent stays expected: the safe direction.
    """
    if not dependents or not prior:
        return frozenset()
    try:
        states = reads.statuses(prior, None)
    except forge.ForgeError as err:
        print(f"  verify: prod's commit {prior[:8]}: {err}; expecting every dependent")
        return frozenset()
    return frozenset(
        name for name in dependents if states.get(STATUS_PREFIX + name) == "failure"
    )


def verify(
    reads: StatusReads,
    target: str,
    prior: str,
    expected: Expected,
    since: datetime | None = None,
    poll_s: int = POLL_S,
) -> Verdict:
    """Wait on T's statuses for the expected names until one fails, all pass or time runs out.

    Args:
        reads: the status reads.
        target: T, the commit `decide` printed. Never the commit that triggered the run.
        prior: `prod`'s commit before the move, or "" on the first run.
        expected: what to wait on.
        since: the earliest update a status on T may carry and still count, so a status
            from an earlier promotion of T reads as none. None counts every status.
        poll_s: the seconds between reads.
    """
    reported = already_failing(reads, prior, expected.dependents)
    names = sorted(expected.names - reported)
    for name in sorted(reported):
        print(
            f"  verify: {STATUS_PREFIX}{name} already read failure on prod's commit "
            f"{prior[:8]}, so it is reported here and not expected"
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
    while True:
        try:
            states = reads.statuses(target, since)
            line = ", ".join(
                f"{n}={states.get(STATUS_PREFIX + n, 'none')}" for n in names
            )
        except forge.ForgeError as err:
            line = f"the statuses could not be read: {err}"
        failed = [n for n in names if states.get(STATUS_PREFIX + n) in FAILED_STATES]
        if failed:
            return Verdict("failed", tuple(failed), tuple(sorted(reported)))
        if all(states.get(STATUS_PREFIX + n) == "success" for n in names):
            return Verdict("good", reported=tuple(sorted(reported)))
        if line != last:
            print(f"  verify: {line}", flush=True)
            last = line
        if reads.clock() - started >= expected.timeout_s:
            missing = [n for n in names if states.get(STATUS_PREFIX + n) != "success"]
            return Verdict("failed", tuple(missing), tuple(sorted(reported)))
        reads.sleep(poll_s)


def report(target: str, verdict: Verdict, github_output: str | None) -> int:
    """Print the last line, append the step outputs, and return the exit code.

    A failed verdict exits 1 so the run reads red, after its outputs are written for `settle`.
    """
    failed = ",".join(verdict.failed)
    print(f"target={target} verdict={verdict.verdict} failed={failed}")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as out:
            out.write(f"verdict={verdict.verdict}\nfailed={failed}\n")
    return OK if verdict.verdict == "good" else FAILED


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
    except (OSError, ValueError, yaml.YAMLError) as err:
        error(f"verify: the bundles decide handed on: {err}")
        return FAILED
    since = moved_at - timedelta(seconds=MOVED_AT_SKEW_S)
    reads = StatusReads(f.github.statuses)
    verdict = verify(reads, args.target, args.prior, expected, since)
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
    """

    stop: tuple[str, str] | None = None
    held: tuple[str, frozenset[str]] | None = None
    prod: str | None = None
    last_good: str | None = None


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
            return Settled(None, held, prod, last_good)
        if digest(HELD_TAG) == prod:
            return Settled(None, held, prod, last_good)
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
