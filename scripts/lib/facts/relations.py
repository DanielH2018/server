"""The status algebra: the spec's Datalog rules as set comprehensions over finite relations.

Recomputed from scratch on every call. The domain is a few hundred units and atoms, so
incremental maintenance would buy nothing and add a source of non-determinism. Nothing here
reads a file — ``lock.build_repo_edb`` builds the ``Edb``. Lint owns an unresolved atom in a
never-verified section; status grades only what verify recorded.
"""

from collections.abc import Mapping
from dataclasses import dataclass

STATUSES = frozenset({"IN", "OUT", "UNVERIFIED", "CONVENTION"})


@dataclass(frozen=True)
class Edb:
    cites: frozenset[tuple[str, str]]
    recorded: Mapping[tuple[str, str], str]
    # The units that HAVE a lock row, read from the lock's keys rather than derived from
    # `recorded`. A row whose every atom failed to hash at verify time records no atom, and
    # deriving the set from the atom pairs would grade it UNVERIFIED instead of OUT.
    recorded_units: frozenset[str]
    current: Mapping[str, str]
    backref: frozenset[tuple[str, str]]
    test_atoms: frozenset[str]


@dataclass(frozen=True)
class Idb:
    missing: frozenset[tuple[str, str]]
    moved: frozenset[tuple[str, str]]
    unrecorded: frozenset[tuple[str, str]]
    one_way: frozenset[tuple[str, str]]
    unverified: frozenset[str]
    out: frozenset[str]
    in_: frozenset[str]


def derive(edb: Edb) -> Idb:
    """Every derived relation, recomputed from scratch.

    missing: a cited atom of a recorded unit that no longer hashes.
    moved: a cited atom whose recorded hash and current hash differ.
    unrecorded: a cited atom of a recorded unit that verify never hashed.
    one_way: a cited test atom carrying no ``# fact:`` back at its unit.
    unverified: a citing unit with no lock row at all.
    out: a unit with a missing, moved or unrecorded atom.
    in_: a citing unit that is neither out nor unverified.
    """
    cites = edb.cites
    # A unit with no lock row at all is UNVERIFIED, not OUT: missing and unrecorded grade
    # only a unit verify has already recorded, even one it recorded no atom for.
    recorded_units = edb.recorded_units
    missing = frozenset(
        (u, a) for u, a in cites if u in recorded_units and a not in edb.current
    )
    moved = frozenset(
        (u, a)
        for u, a in cites
        if (u, a) in edb.recorded
        and a in edb.current
        and edb.recorded[(u, a)] != edb.current[a]
    )
    unrecorded = frozenset(
        (u, a) for u, a in cites if u in recorded_units and (u, a) not in edb.recorded
    )
    one_way = frozenset(
        (u, a) for u, a in cites if a in edb.test_atoms and (a, u) not in edb.backref
    )
    cited_units = frozenset(u for u, _ in cites)
    unverified = cited_units - recorded_units
    out = frozenset(u for u, _ in missing | moved | unrecorded)
    in_ = cited_units - out - unverified
    return Idb(missing, moved, unrecorded, one_way, unverified, out, in_)


def status_of(idb: Idb, unit: str) -> str:
    """One unit's status, by the precedence OUT > UNVERIFIED > IN > CONVENTION.

    The order is what makes a status a verdict rather than a summary: a unit can be in
    several of these sets at once, and the worst one wins. OUT first, because a moved atom
    is a fact that is now wrong. UNVERIFIED next, because a unit nobody has verified cannot
    be graded. A unit that cites nothing is a convention.
    """
    if unit in idb.out:
        return "OUT"
    if unit in idb.unverified:
        return "UNVERIFIED"
    if unit in idb.in_:
        return "IN"
    return "CONVENTION"
