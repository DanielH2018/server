"""H1's forward-only rules in `promote.py decide`: the hold without a reset, the gate, the backstop.

Longhorn supports no downgrade, and MetalLB's CRDs carry a conversion webhook, so resetting
`prod` after a failed bump would re-apply an older vendored file over upgraded objects. Three
rules keep a promotion from doing that. The plan is
docs/target-architecture/host/h1-cluster-config-to-flux.md, *Forward-only tags and the promote
rule* and *Longhorn and MetalLB bumps gate at the merge*; contracts.md C2 states them as *The
forward-only gates* and *A hold without a reset*.

THE HOLD WITHOUT A RESET. `no_reset_names` names the tags under which a failed revision holds
and leaves `prod`. It is the one predicate D5.2 extends with snapshot roles: a failed revision
holds without reset when its changed tags include a forward-only tag (or, from D5.2, a
snapshot role). A tag changes, against `last-good`'s bundle, when its `footprint` in
`digests.json` differs, when `last-good` lacks it, or when T lacks it. A change under
`_unclassified`, or a bundle with no readable `digests.json`, counts as every tag changed. A
role is forward-only when its `defaults/main.yml` sets `flux_forward_only: true` at T or at the
lower bound's commit, so a T that deletes the role still counts it. `settle` reads the names
from `decide`'s `no_reset` output and writes DEPLOY_HOLD as T's SHA alone.

THE GATE. Every commit C in (`last-good`, T] that changes a forward-only pin
(`cluster_pins.PINS`, read from whichever role file holds it at each SHA) must map, through
GitHub's commit-to-pulls lookup, to the merged PR whose merge commit is C, and that PR's head
must carry a successful `homelab/upgrade-gates` status, which `land.sh` posts (H1.1g). For a C
in (`prod`, T], the commits this move newly applies, the status must be at most
GATE_STATUS_MAX_AGE old. No PR, no status, a status that is not `success` and a stale one each
hold; a lookup that fails is an error.

THE BACKSTOP. (`prod`, T] holds no more than one change to each pin, and
`k3s_longhorn_version` moves at most one minor release between `prod` and T. It catches a
merge that bypassed `land.sh`'s land rule.

Every read is a `promote.Reads` field, so this module reaches GitHub only through `forge.py`
(scripts/deploy_tools/tests/test_promote_forge_boundary.py).
"""

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

import yaml

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
# `forge` and `changed_set` import bare, from scripts/deploy_tools.
_sys.path.insert(1, str(_Path(__file__).resolve().parents[1]))
from lib import yaml_fast
from lib.git import git
from validate.cluster_pins import PINS, Pin, Unjudged, version

import forge
from changed_set import footprints
from deploy_tools.promote_lib.prune import ReadError

FORWARD_ONLY_FLAG = "flux_forward_only"
# A k8s role's directory name is its deploy tag, which is the name `digests.json` keys.
ROLE_DEFAULTS = ":(glob)ansible/roles/k8s/*/defaults/main.yml"
UNCLASSIFIED = "_unclassified"
GATE_STATUS_CONTEXT = "homelab/upgrade-gates"
# A status older than this describes a drill age and a volume health from before a hold or
# an outage came between the merge and the promotion (*A gate status goes stale*).
GATE_STATUS_MAX_AGE = timedelta(hours=6)


# -- the hold without a reset ----------------------------------------------------------------


def changed_tags(target: object, base: object) -> frozenset[str] | None:
    """The tags whose footprint differs between two parsed `digests.json`, or None for unknown.

    A tag one side lacks counts as changed. A change under `_unclassified` counts as every
    tag on either side changed. None when either side is missing or malformed.
    """
    ours, theirs = footprints(target), footprints(base)
    if ours is None or theirs is None:
        return None
    names = (ours.keys() | theirs.keys()) - {UNCLASSIFIED}
    if ours.get(UNCLASSIFIED) != theirs.get(UNCLASSIFIED):
        return frozenset(names)
    return frozenset(n for n in names if ours.get(n) != theirs.get(n))


def forward_only_roles(repo: Path, sha: str) -> frozenset[str]:
    """The k8s roles whose `defaults/main.yml` sets FORWARD_ONLY_FLAG to true at `sha`.

    Raises:
        ReadError: a git read failed, or a defaults file that names the flag is not YAML.
    """
    hits = git(
        "grep", "-l", "-E", f"^{FORWARD_ONLY_FLAG}:", sha, "--", ROLE_DEFAULTS,
        cwd=repo, check=False,
    )  # fmt: skip
    if hits.returncode not in (0, 1):
        raise ReadError(f"git grep {FORWARD_ONLY_FLAG} at {sha}: {hits.stderr.strip()}")
    roles = set()
    for line in hits.stdout.splitlines():
        path = line.split(":", 1)[1]
        shown = git("show", f"{sha}:{path}", cwd=repo, check=False)
        if shown.returncode != 0:
            raise ReadError(f"git show {sha}:{path}: {shown.stderr.strip()}")
        try:
            data = yaml_fast.safe_load(shown.stdout)
        except yaml.YAMLError as err:
            raise ReadError(f"{path} at {sha}: {err}") from err
        if isinstance(data, dict) and data.get(FORWARD_ONLY_FLAG) is True:
            roles.add(path.split("/")[3])
    return frozenset(roles)


def no_reset_names(
    changed: frozenset[str] | None, forward_only: frozenset[str]
) -> frozenset[str]:
    """The changed tags under which a failed revision holds without resetting `prod`.

    Args:
        changed: `changed_tags` against `last-good`, or None when the digests were
            unreadable, which counts every tag as changed.
        forward_only: the tags whose role sets FORWARD_ONLY_FLAG. D5.2 passes the snapshot
            roles here too.
    """
    return forward_only if changed is None else changed & forward_only


# -- the gate and the backstop ---------------------------------------------------------------


class GateReads(Protocol):
    """The reads the gate makes. `promote.Reads` carries each one as a field.

    Attributes:
        walk: the commits after the first one up to and including the second, newest first.
        pins: each key in PINS mapped to its pin at a commit.
        pulls: the PRs GitHub associates with a commit.
        gate_status: the newest `homelab/upgrade-gates` status on a commit as
            `(state, updated_at)`, or None.
        now: the wall-clock time the age bound is measured from.
    """

    # Properties, so a frozen dataclass's read-only fields satisfy the protocol.
    @property
    def walk(self) -> Callable[[str | None, str], list[str]]: ...
    @property
    def pins(self) -> Callable[[str], Mapping[str, Pin | None]]: ...
    @property
    def pulls(self) -> Callable[[str], list[dict]]: ...
    @property
    def gate_status(self) -> Callable[[str], tuple[str, datetime] | None]: ...
    @property
    def now(self) -> Callable[[], datetime]: ...


def _value(pins: Mapping[str, Pin | None], key: str) -> str | None:
    pin = pins.get(key)
    return pin.value if pin is not None else None


def pin_changes(
    shas: list[str], pins: Callable[[str], Mapping[str, Pin | None]]
) -> list[tuple[str, str]]:
    """Each `(commit, pin)` where the pin's value differs from the commit before it.

    Args:
        shas: oldest first. The first is the range's lower bound and is never reported.
        pins: reads the pins at a commit.
    """
    at = [pins(sha) for sha in shas]
    return [
        (shas[i], key)
        for i in range(1, len(shas))
        for key in PINS
        if _value(at[i - 1], key) != _value(at[i], key)
    ]


def _head_of(sha: str, pulls: list[dict]) -> tuple[str, str] | None:
    """The number and head SHA of the merged PR whose merge commit is `sha`, if any.

    Raises:
        forge.ForgeError: an entry is not a PR object, so the lookup's answer is unreadable.
    """
    for pr in pulls:
        head = pr.get("head") if isinstance(pr, dict) else None
        if not isinstance(head, dict):
            raise forge.ForgeError(f"pull requests for {sha}: an entry has no head")
        if pr.get("merge_commit_sha") == sha and isinstance(head.get("sha"), str):
            return str(pr.get("number")), head["sha"]
    return None


def _gate_problem(reads: GateReads, sha: str, key: str, applies: bool) -> str:
    """Why commit `sha`, which changes `key`, lacks its gate status, or "" when it has one."""
    pr = _head_of(sha, reads.pulls(sha))
    if pr is None:
        return (
            f"{sha[:9]} changes {key} and maps to no merged PR, so it was never gated"
        )
    number, head = pr
    status = reads.gate_status(head)
    if status is None:
        return f"{sha[:9]} (PR #{number}) changes {key} with no {GATE_STATUS_CONTEXT} status"
    state, updated = status
    if state != "success":
        return f"{sha[:9]} (PR #{number}) has {GATE_STATUS_CONTEXT}={state}"
    age = reads.now() - updated
    if applies and age > GATE_STATUS_MAX_AGE:
        hours = age.total_seconds() / 3600
        return (
            f"{sha[:9]} (PR #{number}) has a {GATE_STATUS_CONTEXT} status {hours:.1f}h old, "
            f"over the {GATE_STATUS_MAX_AGE.total_seconds() / 3600:.0f}h bound; "
            f"re-run `land.sh --regate {number}`"
        )
    return ""


def _backstop(
    applied: list[tuple[str, str]],
    at_prod: Mapping[str, Pin | None],
    at_target: Mapping[str, Pin | None],
) -> list[str]:
    """Each backstop refusal over (`prod`, T]."""
    found = []
    for key, minor_steps in PINS.items():
        hits = [sha for sha, k in applied if k == key]
        if len(hits) > 1:
            found.append(
                f"(prod, T] changes {key} in {len(hits)} commits "
                f"({', '.join(h[:9] for h in hits)}), which would apply as one hop"
            )
        old, new = at_prod.get(key), at_target.get(key)
        if (
            minor_steps
            and old is not None
            and new is not None
            and old.value != new.value
        ):
            was, now = version(old), version(new)
            if now[0] != was[0] or abs(now[1] - was[1]) > 1:
                found.append(
                    f"{key} moves from {old.value} at prod to {new.value} at T, more "
                    "than one minor release"
                )
    return found


def gate(
    reads: GateReads, lower: str | None, prod: str | None, target: str, window: int
) -> list[str]:
    """Every reason the forward-only gate or the backstop holds T, or [] when T passes.

    Args:
        reads: the reads.
        lower: `last-good`'s commit, else `prod`'s, else None on a first run.
        prod: `prod`'s commit, or None on a first run.
        target: T.
        window: how many commits a first run reads back from T, its walk's own limit.

    Raises:
        forge.ForgeError: a pull-request or status lookup failed.
        ReadError: a pin could not be read, or its value is not a version.
    """
    try:
        return _gate(reads, lower, prod, target, window)
    except Unjudged as err:
        raise ReadError(f"the forward-only pins: {err}") from err


def _gate(
    reads: GateReads, lower: str | None, prod: str | None, target: str, window: int
) -> list[str]:
    newest_first = reads.walk(lower, target)
    if lower is None:
        # DECIDED: a first run has no `last-good` and no `prod` to bound the range, so it
        # reads the same window its walk examined and takes the oldest commit in it as the
        # lower bound. The first run predates every forward-only pin by construction.
        newest_first = newest_first[:window]
        lower, newest_first = newest_first[-1], newest_first[:-1]
    shas = [lower, *reversed(newest_first)]
    changes = pin_changes(shas, reads.pins)
    if not changes:
        return []
    after_prod = shas.index(prod) + 1 if prod in shas else 1
    newly_applied = set(shas[after_prod:])
    found = [
        problem
        for sha, key in changes
        if (problem := _gate_problem(reads, sha, key, sha in newly_applied))
    ]
    applied = [(sha, key) for sha, key in changes if sha in newly_applied]
    at_prod = reads.pins(shas[after_prod - 1])
    found += _backstop(applied, at_prod, reads.pins(target))
    return found
