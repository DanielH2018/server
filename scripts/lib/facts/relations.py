"""The status algebra: the spec's Datalog rules as set comprehensions over finite relations.

Recomputed from scratch on every call. The domain is a few hundred units and atoms, so
incremental maintenance would buy nothing and add a source of non-determinism. Nothing here
reads a file — ``lock.build_repo_edb`` and (slice 4) the memory reader build the ``Edb``.
"""

from collections.abc import Mapping
from dataclasses import dataclass

STATUSES = frozenset({"IN", "OUT", "UNKNOWN", "UNDECLARED", "CONVENTION"})


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
    out: frozenset[str]
    in_: frozenset[str]
    supported_by_out: frozenset[str]


def derive(edb: Edb) -> Idb:
    cites = edb.cites
    # A probe with no answer is neither current nor missing: it is unknown, decided below.
    missing = frozenset(
        (u, a)
        for u, a in cites
        if a not in edb.current and a not in edb.transport_failed
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
        if (u, a) not in edb.recorded and a not in edb.transport_failed
    )
    unknown = frozenset(
        u for u, a in cites if a in edb.live and a in edb.transport_failed
    )
    one_way = frozenset(
        (u, a) for u, a in cites if a in edb.test_atoms and (a, u) not in edb.backref
    )
    cited_units = frozenset(u for u, _ in cites)
    undeclared = edb.memory_units - cited_units
    out = frozenset(u for u, _ in missing | moved | unrecorded)
    in_ = cited_units - out - unknown
    supported_by_out = frozenset(u for u, f in edb.links if u in in_ and f in out)
    return Idb(
        missing,
        moved,
        unrecorded,
        one_way,
        unknown,
        undeclared,
        out,
        in_,
        supported_by_out,
    )


def status_of(edb: Edb, idb: Idb, unit: str) -> str:
    if unit in idb.out:
        return "OUT"
    if unit in idb.unknown:
        return "UNKNOWN"
    if unit in idb.in_:
        return "IN"
    return "UNDECLARED" if unit in edb.memory_units else "CONVENTION"
