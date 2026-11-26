"""Generated templates inside ADR-0028's subset pass the census, and one construct outside fails it.

Each template is a run of pieces, and each piece names its constructs in one of the positions
`dialect.constructs` reads. The property tests run under the profile
`tests/hypothesis_profiles.py` loads.
"""

from typing import NamedTuple

from hypothesis import given
from hypothesis import strategies as st

from renderer.renderer_lib.dialect import (
    FILTERS,
    LOOKUPS,
    TESTS,
    Construct,
    constructs,
    off_the_list,
)


_KEYWORDS = frozenset(
    {"and", "or", "not", "in", "is", "if", "else", "true", "false", "none"}
)
_ON_LIST = FILTERS | TESTS | LOOKUPS | _KEYWORDS


class Piece(NamedTuple):
    """A fragment of template text and the constructs it names."""

    text: str
    names: frozenset[Construct]


def _filter(name: str) -> Piece:
    return Piece(f"{{{{ x | {name} }}}}", frozenset({Construct("filter", name)}))


def _test(name: str) -> Piece:
    return Piece(
        f"{{% if x is {name} %}}y{{% endif %}}", frozenset({Construct("test", name)})
    )


def _lookup(name: str) -> Piece:
    return Piece(
        f"{{{{ lookup('{name}', 'p') }}}}", frozenset({Construct("lookup", name)})
    )


def _reject_by_name(name: str) -> Piece:
    return Piece(
        f"{{{{ xs | reject('{name}', ':') | list }}}}",
        frozenset(
            {
                Construct("filter", "reject"),
                Construct("filter", "list"),
                Construct("test", name),
            }
        ),
    )


def _selectattr_by_name(name: str) -> Piece:
    return Piece(
        f"{{{{ xs | selectattr('a', '{name}') | list }}}}",
        frozenset(
            {
                Construct("filter", "selectattr"),
                Construct("filter", "list"),
                Construct("test", name),
            }
        ),
    )


def _map_by_name(name: str) -> Piece:
    return Piece(
        f"{{{{ xs | map('{name}') | list }}}}",
        frozenset(
            {
                Construct("filter", "map"),
                Construct("filter", "list"),
                Construct("filter", name),
            }
        ),
    )


_FILTER_POSITIONS = (_filter, _map_by_name)
_TEST_POSITIONS = (_test, _reject_by_name, _selectattr_by_name)


def _placed(positions: tuple, names: frozenset[str]) -> st.SearchStrategy[Piece]:
    return st.builds(
        lambda place, name: place(name),
        st.sampled_from(positions),
        st.sampled_from(sorted(names)),
    )


in_subset_pieces = st.one_of(
    _placed(_FILTER_POSITIONS, FILTERS),
    _placed(_TEST_POSITIONS, TESTS),
    _placed((_lookup,), LOOKUPS),
)

off_names = st.from_regex(r"[a-z][a-z_]{0,11}", fullmatch=True).filter(
    lambda name: name not in _ON_LIST
)


def _off_text(kind: str, name: str) -> st.SearchStrategy[str]:
    positions = {
        "filter": _FILTER_POSITIONS,
        "test": _TEST_POSITIONS,
        "lookup": (_lookup,),
    }[kind]
    return st.sampled_from(positions).map(lambda place: place(name).text)


off_by_name = st.tuples(
    st.sampled_from(["filter", "test", "lookup"]), off_names
).flatmap(lambda pair: st.tuples(st.just(Construct(*pair)), _off_text(*pair)))

off_by_variable = st.sampled_from(
    [
        (Construct("filter", "<dynamic>"), "{{ xs | map(f) | list }}"),
        (Construct("test", "<dynamic>"), "{{ xs | reject(t) | list }}"),
        (Construct("test", "<dynamic>"), "{{ xs | selectattr('a', t) | list }}"),
        (Construct("lookup", "<dynamic>"), "{{ lookup(kind, 'p') }}"),
    ]
)


def _select_by_name(name: str) -> Piece:
    return Piece(
        f"{{{{ xs | select('{name}') }}}}",
        frozenset({Construct("filter", "select"), Construct("test", name)}),
    )


def _rejectattr_by_name(name: str) -> Piece:
    return Piece(
        f"{{{{ xs | rejectattr('a', '{name}') }}}}",
        frozenset({Construct("filter", "rejectattr"), Construct("test", name)}),
    )


off_carriers = st.builds(
    lambda place, name: place(name),
    st.sampled_from([_select_by_name, _rejectattr_by_name]),
    off_names,
)


@given(st.lists(in_subset_pieces, min_size=1, max_size=6))
def test_a_template_inside_the_subset_names_its_constructs_and_passes(pieces):
    source = "\n".join(piece.text for piece in pieces)
    assert constructs(source) == set().union(*(piece.names for piece in pieces))
    assert off_the_list(source) == set()


@given(
    st.lists(in_subset_pieces, max_size=5),
    st.one_of(off_by_name, off_by_variable),
    st.data(),
)
def test_a_template_one_construct_outside_the_subset_is_refused_by_that_construct(
    pieces, off, data
):
    construct, text = off
    texts = [piece.text for piece in pieces]
    texts.insert(data.draw(st.integers(0, len(texts))), text)
    assert off_the_list("\n".join(texts)) == {construct}


@given(st.lists(in_subset_pieces, max_size=5), off_carriers, st.data())
def test_an_off_list_filter_that_takes_a_test_by_name_is_refused_with_that_test(
    pieces, carrier, data
):
    texts = [piece.text for piece in pieces]
    texts.insert(data.draw(st.integers(0, len(texts))), carrier.text)
    assert off_the_list("\n".join(texts)) == carrier.names
