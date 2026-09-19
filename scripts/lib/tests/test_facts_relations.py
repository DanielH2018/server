"""The spec's IDB rules, one clean/flagged pair each, over hand-built relations."""

from collections.abc import Mapping

from lib.facts.relations import STATUSES, Edb, derive, status_of

U, A, T = "CLAUDE.md#Sec", "m.py:run", "t/test_m.py::test_run"


def _edb(
    *,
    cites: frozenset[tuple[str, str]] = frozenset({(U, A)}),
    recorded: Mapping[tuple[str, str], str] | None = None,
    current: Mapping[str, str] | None = None,
    live: frozenset[str] = frozenset(),
    transport_failed: frozenset[str] = frozenset(),
    backref: frozenset[tuple[str, str]] = frozenset(),
    links: frozenset[tuple[str, str]] = frozenset(),
    memory_units: frozenset[str] = frozenset(),
    test_atoms: frozenset[str] = frozenset(),
) -> Edb:
    if recorded is None:
        recorded = {(U, A): "h1"}
    if current is None:
        current = {A: "h1"}
    return Edb(
        cites=cites,
        recorded=recorded,
        current=current,
        live=live,
        transport_failed=transport_failed,
        backref=backref,
        links=links,
        memory_units=memory_units,
        test_atoms=test_atoms,
    )


def test_matching_hashes_are_in():
    e = _edb()
    assert status_of(e, derive(e), U) == "IN"


def test_moved_atom_is_out():
    e = _edb(current={A: "h2"})
    i = derive(e)
    assert (U, A) in i.moved and U in i.out and status_of(e, i, U) == "OUT"


def test_missing_atom_is_out():
    e = _edb(current={})
    i = derive(e)
    assert (U, A) in i.missing and status_of(e, i, U) == "OUT"


def test_unrecorded_citation_is_out():
    other = "m.py:other"
    e = _edb(
        cites=frozenset({(U, A), (U, other)}),
        recorded={(U, A): "h1"},
        current={A: "h1", other: "h9"},
    )
    i = derive(e)
    assert (U, other) in i.unrecorded and status_of(e, i, U) == "OUT"


def test_never_verified_unit_is_unverified():
    e = _edb(recorded={})
    i = derive(e)
    assert U not in i.out and status_of(e, i, U) == "UNVERIFIED"


def test_missing_atom_on_a_never_verified_unit_is_unverified_not_out():
    e = _edb(recorded={}, current={})
    i = derive(e)
    assert U not in i.out and status_of(e, i, U) == "UNVERIFIED"


def test_probe_transport_failure_is_unknown_not_out():
    p = "probe.py kuma-drift"
    e = _edb(
        cites=frozenset({(U, A), (U, p)}),
        live=frozenset({p}),
        transport_failed=frozenset({p}),
    )
    i = derive(e)
    assert U in i.unknown and U not in i.out and status_of(e, i, U) == "UNKNOWN"


def test_probe_that_answered_is_in():
    p = "probe.py kuma-drift"
    e = _edb(
        cites=frozenset({(U, A), (U, p)}),
        live=frozenset({p}),
        recorded={(U, A): "h1", (U, p): "s1"},
        current={A: "h1", p: "s1"},
    )
    assert status_of(e, derive(e), U) == "IN"


def test_test_atom_without_backref_is_one_way_but_still_in():
    e = _edb(
        cites=frozenset({(U, T)}),
        recorded={(U, T): "h"},
        current={T: "h"},
        test_atoms=frozenset({T}),
    )
    i = derive(e)
    assert (U, T) in i.one_way and status_of(e, i, U) == "IN"


def test_test_atom_with_backref_is_not_one_way():
    e = _edb(
        cites=frozenset({(U, T)}),
        recorded={(U, T): "h"},
        current={T: "h"},
        test_atoms=frozenset({T}),
        backref=frozenset({(T, U)}),
    )
    assert not derive(e).one_way


def test_memory_unit_with_no_citation_is_undeclared():
    m = "some-lesson"
    e = _edb(memory_units=frozenset({m}))
    i = derive(e)
    assert m in i.undeclared and status_of(e, i, m) == "UNDECLARED"


def test_repo_section_with_no_citation_is_convention():
    e = _edb()
    assert status_of(e, derive(e), "CLAUDE.md#Commit style") == "CONVENTION"


def test_supported_by_out_is_one_level():
    v, w = "CLAUDE.md#V", "CLAUDE.md#W"
    e = _edb(
        cites=frozenset({(U, A), (v, A), (w, A)}),
        recorded={(U, A): "h1", (v, A): "h2", (w, A): "h1"},
        links=frozenset({(U, v), (w, U)}),
    )
    i = derive(e)
    assert U in i.supported_by_out  # U links to v, v is OUT
    assert w not in i.supported_by_out  # w links to U, U is IN — no cascade


def test_transport_failure_on_a_non_live_atom_is_out():
    a_failed = "some-atom-that-failed-but-is-not-live"
    e = _edb(
        cites=frozenset({(U, a_failed)}),
        recorded={(U, a_failed): "h1"},
        current={},
        transport_failed=frozenset({a_failed}),
    )
    i = derive(e)
    assert (U, a_failed) in i.missing and status_of(e, i, U) == "OUT"


def test_out_beats_unknown():
    p = "probe.py kuma-drift"
    e = _edb(
        cites=frozenset({(U, A), (U, p)}),
        recorded={(U, A): "h1", (U, p): "s1"},
        current={A: "h2"},
        live=frozenset({p}),
        transport_failed=frozenset({p}),
    )
    i = derive(e)
    assert U in i.out and U in i.unknown and status_of(e, i, U) == "OUT"


def test_status_census():
    assert STATUSES == frozenset(
        {"IN", "OUT", "UNKNOWN", "UNVERIFIED", "UNDECLARED", "CONVENTION"}
    )
