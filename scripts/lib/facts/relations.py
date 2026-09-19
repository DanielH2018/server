"""The status algebra: the spec's Datalog rules as set comprehensions over finite relations.

Recomputed from scratch on every call. The domain is a few hundred units and atoms, so
incremental maintenance would buy nothing and add a source of non-determinism. Nothing here
reads a file — ``lock.build_repo_edb`` and (slice 4) the memory reader build the ``Edb``.
Lint owns an unresolved atom in a never-verified section; status grades only what verify
recorded.
"""

from collections.abc import Mapping
from dataclasses import dataclass

STATUSES = frozenset({"IN", "OUT", "UNKNOWN", "UNVERIFIED", "UNDECLARED", "CONVENTION"})


@dataclass(frozen=True)
class Edb:
    cites: frozenset[tuple[str, str]]
    recorded: Mapping[tuple[str, str], str]
    current: Mapping[str, str]
    live: frozenset[str]
    transport_failed: frozenset[str]
    backref: frozenset[tuple[str, str]]
    links: frozenset[tuple[str, str]]
    memory_units: frozenset[str]
    test_atoms: frozenset[str]


@dataclass(frozen=True)
class Idb:
    missing: frozenset[tuple[str, str]]
    moved: frozenset[tuple[str, str]]
    unrecorded: frozenset[tuple[str, str]]
    one_way: frozenset[tuple[str, str]]
    unknown: frozenset[str]
    undeclared: frozenset[str]
    unverified: frozenset[str]
    out: frozenset[str]
    in_: frozenset[str]
    supported_by_out: frozenset[str]


def derive(edb: Edb) -> Idb:
    cites = edb.cites
    # A unit with no lock row at all is UNVERIFIED, not OUT: missing and unrecorded grade
    # only a unit verify has already recorded at least one atom for.
    recorded_units = frozenset(u for u, _a in edb.recorded)
    # A live probe with no answer is unknown, not missing: only live probes earn UNKNOWN.
    # A non-live atom in transport_failed is missing/unrecorded, making the unit OUT.
    missing = frozenset(
        (u, a)
        for u, a in cites
        if u in recorded_units
        and a not in edb.current
        and not (a in edb.live and a in edb.transport_failed)
    )
    moved = frozenset(
        (u, a)
        for u, a in cites
        if (u, a) in edb.recorded
        and a in edb.current
        and edb.recorded[(u, a)] != edb.current[a]
    )
    unrecorded = frozenset(
        (u, a)
        for u, a in cites
        if u in recorded_units
        and (u, a) not in edb.recorded
        and not (a in edb.live and a in edb.transport_failed)
    )
    unknown = frozenset(
        u for u, a in cites if a in edb.live and a in edb.transport_failed
    )
    one_way = frozenset(
        (u, a) for u, a in cites if a in edb.test_atoms and (a, u) not in edb.backref
    )
    cited_units = frozenset(u for u, _ in cites)
    undeclared = edb.memory_units - cited_units
    unverified = cited_units - recorded_units
    out = frozenset(u for u, _ in missing | moved | unrecorded)
    in_ = cited_units - out - unknown - unverified
    supported_by_out = frozenset(u for u, f in edb.links if u in in_ and f in out)
    return Idb(
        missing,
        moved,
        unrecorded,
        one_way,
        unknown,
        undeclared,
        unverified,
        out,
        in_,
        supported_by_out,
    )


def status_of(edb: Edb, idb: Idb, unit: str) -> str:
    """One unit's status, by the precedence OUT > UNVERIFIED > UNKNOWN > IN > UNDECLARED/CONVENTION.

    The order is what makes a status a verdict rather than a summary: a unit can be in
    several of these sets at once, and the worst one wins. OUT first, because a moved atom
    is a fact that is now wrong. UNVERIFIED next, because a unit nobody has verified cannot
    be graded. UNKNOWN next, because a live probe with no answer is a gap in the evidence,
    not in the claim. IN last of the graded four. A unit that cites nothing is a convention
    in the repo store, and UNDECLARED in the memory store.
    """
    status = _status_of(edb, idb, unit)
    assert status in STATUSES, status
    return status


def _status_of(edb: Edb, idb: Idb, unit: str) -> str:
    if unit in idb.out:
        return "OUT"
    if unit in idb.unverified:
        return "UNVERIFIED"
    if unit in idb.unknown:
        return "UNKNOWN"
    if unit in idb.in_:
        return "IN"
    return "UNDECLARED" if unit in edb.memory_units else "CONVENTION"
