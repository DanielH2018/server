"""The spec's IDB rules, one clean/flagged pair each, over hand-built relations."""

from collections.abc import Mapping

from lib.facts.relations import STATUSES, Edb, derive, status_of

U, A, T = "CLAUDE.md#Sec", "m.py:run", "t/test_m.py::test_run"


def _edb(
    *,
    cites: frozenset[tuple[str, str]] = frozenset({(U, A)}),
    recorded: Mapping[tuple[str, str], str] | None = None,
    recorded_units: frozenset[str] | None = None,
    current: Mapping[str, str] | None = None,
    backref: frozenset[tuple[str, str]] = frozenset(),
    test_atoms: frozenset[str] = frozenset(),
) -> Edb:
    if recorded is None:
        recorded = {(U, A): "h1"}
    if current is None:
        current = {A: "h1"}
    if recorded_units is None:
        recorded_units = frozenset(u for u, _a in recorded)
    return Edb(
        cites=cites,
        recorded=recorded,
        recorded_units=recorded_units,
        current=current,
        backref=backref,
        test_atoms=test_atoms,
    )


def test_matching_hashes_are_in():
    e = _edb()
    assert status_of(derive(e), U) == "IN"


def test_moved_atom_is_out():
    e = _edb(current={A: "h2"})
    i = derive(e)
    assert (U, A) in i.moved and U in i.out and status_of(i, U) == "OUT"


def test_missing_atom_is_out():
    e = _edb(current={})
    i = derive(e)
    assert (U, A) in i.missing and status_of(i, U) == "OUT"


def test_unrecorded_citation_is_out():
    other = "m.py:other"
    e = _edb(
        cites=frozenset({(U, A), (U, other)}),
        recorded={(U, A): "h1"},
        current={A: "h1", other: "h9"},
    )
    i = derive(e)
    assert (U, other) in i.unrecorded and status_of(i, U) == "OUT"


def test_never_verified_unit_is_unverified():
    e = _edb(recorded={})
    i = derive(e)
    assert U not in i.out and status_of(i, U) == "UNVERIFIED"


def test_missing_atom_on_a_never_verified_unit_is_unverified_not_out():
    e = _edb(recorded={}, current={})
    i = derive(e)
    assert U not in i.out and status_of(i, U) == "UNVERIFIED"


def test_test_atom_without_backref_is_one_way_but_still_in():
    e = _edb(
        cites=frozenset({(U, T)}),
        recorded={(U, T): "h"},
        current={T: "h"},
        test_atoms=frozenset({T}),
    )
    i = derive(e)
    assert (U, T) in i.one_way and status_of(i, U) == "IN"


def test_test_atom_with_backref_is_not_one_way():
    e = _edb(
        cites=frozenset({(U, T)}),
        recorded={(U, T): "h"},
        current={T: "h"},
        test_atoms=frozenset({T}),
        backref=frozenset({(T, U)}),
    )
    assert not derive(e).one_way


def test_repo_section_with_no_citation_is_convention():
    e = _edb()
    assert status_of(derive(e), "CLAUDE.md#Commit style") == "CONVENTION"


def test_status_census():
    assert STATUSES == frozenset({"IN", "OUT", "UNVERIFIED", "CONVENTION"})


def test_a_recorded_unit_with_no_recorded_atom_is_out_not_unverified():
    # Verify wrote the row but could hash none of the section's atoms, so the row is empty.
    e = _edb(recorded={}, recorded_units=frozenset({U}), current={})
    i = derive(e)
    assert (U, A) in i.unrecorded and status_of(i, U) == "OUT"
