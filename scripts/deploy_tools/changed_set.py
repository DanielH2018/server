"""The changed set: which Kustomizations a commit's bundle changes, from two `digests.json` files.

Contract C2 in docs/target-architecture/contracts.md defines it, and contract C1 defines the
file. Each bundle carries `digests.json` at its root. It maps each deploy tag and each shared
kind to two SHA-256 digests, `own` and `footprint`, plus the reserved key `_unclassified` for a
bundle path that counts for no name. The changed set between an older bundle and a newer one
holds:

  - each name whose `footprint` differs, including a name the newer file adds;
  - every Kustomization whose `dependsOn` chain reaches one of those names;
  - every name, when `_unclassified` differs.

It leaves out a name absent from the newer file, which the newer commit removes, and every
suspended Kustomization.

A file that is missing or malformed counts as every name changed, and the result says it
widened. Widening is the safe direction: a reader waits on more names than the commit moved,
never on fewer.

THREE CALLERS, ONE FUNCTION. `promote.py land-wait` reads it for C6's wait (D2.3). D2.5's
`promote.py verify` and D4.3's timeout derivation call the same `changed_set`, and none of
them derives the set a second time (docs/target-architecture/deploy/d2-first-leaf.md, *Steps,
as PRs*, D2.5). It is pure: the callers read the files and the Kustomizations themselves.

Run: imported only. `uv run pytest scripts/deploy_tools/tests/test_changed_set.py`
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# The reserved key C1 records a path under when it counts for no name.
UNCLASSIFIED = "_unclassified"
# The root Kustomization. Every reader expects it beside the changed set, so it is never a
# member of the set itself.
ROOT = "homelab"


@dataclass(frozen=True)
class ChangedSet:
    """What `changed_set` concluded.

    Attributes:
        names: the Kustomizations the newer bundle changes, `ROOT` excluded.
        widened: why every name counts as changed, or "" when the digests decided.
    """

    names: frozenset[str]
    widened: str = ""


def footprints(digests: object) -> dict[str, str] | None:
    """Each name's `footprint` digest in a parsed `digests.json`, or None when it is malformed.

    The file must be a JSON object mapping each name to an object whose `footprint` is a
    string. `_unclassified` is read the same way.
    """
    if not isinstance(digests, Mapping):
        return None
    out: dict[str, str] = {}
    for name, entry in digests.items():
        footprint = entry.get("footprint") if isinstance(entry, Mapping) else None
        if not isinstance(name, str) or not isinstance(footprint, str):
            return None
        out[name] = footprint
    return out


def dependents(
    names: Iterable[str], depends_on: Mapping[str, Iterable[str]]
) -> set[str]:
    """`names` plus every Kustomization whose `dependsOn` chain reaches one of them."""
    reached = set(names)
    grew = True
    while grew:
        grew = False
        for name, deps in depends_on.items():
            if name not in reached and reached.intersection(deps):
                reached.add(name)
                grew = True
    return reached


def changed_set(
    new: object,
    old: object,
    depends_on: Mapping[str, Iterable[str]],
    suspended: Iterable[str] = (),
) -> ChangedSet:
    """The changed set between the bundle holding `old` and the newer one holding `new`.

    Args:
        new: the newer bundle's parsed `digests.json`, or None when it could not be read.
        old: the older bundle's, or None when it could not be read.
        depends_on: each Kustomization's `dependsOn` names. Its keys are also every name the
            set widens to when `new` gives none.
        suspended: the Kustomizations left out whatever changed.
    """
    left_out = set(suspended) | {ROOT}
    new_prints = None if new is None else footprints(new)
    old_prints = None if old is None else footprints(old)
    widened = ""
    if new is None:
        widened = "the newer digests.json could not be read"
    elif new_prints is None:
        widened = "the newer digests.json is malformed"
    elif old is None:
        widened = "the older digests.json could not be read"
    elif old_prints is None:
        widened = "the older digests.json is malformed"
    elif new_prints.get(UNCLASSIFIED) != old_prints.get(UNCLASSIFIED):
        widened = f"{UNCLASSIFIED} changed"
    universe = set(new_prints or depends_on) - {UNCLASSIFIED}
    if widened or new_prints is None or old_prints is None:
        return ChangedSet(frozenset(universe - left_out), widened)
    moved = {
        name
        for name, footprint in new_prints.items()
        if name != UNCLASSIFIED and old_prints.get(name) != footprint
    }
    # A dependent the newer bundle removes is not expected either.
    reached = dependents(moved, depends_on) & (universe | moved)
    return ChangedSet(frozenset(reached - left_out))
