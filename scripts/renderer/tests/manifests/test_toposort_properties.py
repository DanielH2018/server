"""`toposort_containers` matches a naive stable sort on generated graphs.

Names come from a small pool so edges collide and close cycles, and some deps name no
entry. The property tests run under the profile `tests/hypothesis_profiles.py` loads.
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from renderer.template_filters.toposort import DependencyCycle, toposort_containers

_POOL = ("a", "b", "c", "d", "e", "f")
_names = st.sampled_from(_POOL)
_deps = st.dictionaries(_names, st.lists(st.sampled_from((*_POOL, "ghost", "gone"))))


def _naive_order(names: list[str], deps: dict[str, list[str]]) -> list[str] | None:
    """Place the earliest entry whose listed deps are all placed; None on a cycle."""
    placed: list[str] = []
    waiting = list(names)
    while waiting:
        for name in waiting:
            if all(d in placed or d not in names for d in deps.get(name, [])):
                placed.append(name)
                waiting.remove(name)
                break
        else:
            return None
    return placed


@given(names=st.lists(_names, unique=True), deps=_deps)
def test_order_is_the_stable_topological_one_or_a_cycle(names, deps):
    entries = [{"name": name} for name in names]
    expected = _naive_order(names, deps)
    if expected is None:
        with pytest.raises(DependencyCycle):
            toposort_containers(entries, deps)
    else:
        assert toposort_containers(entries, deps) == [{"name": n} for n in expected]


@given(names=st.lists(_names, min_size=1), deps=_deps, data=st.data())
def test_a_repeated_name_is_refused_by_name(names, deps, data):
    repeated = data.draw(st.sampled_from(names))
    entries = [{"name": name} for name in [*names, repeated]]
    with pytest.raises(ValueError, match=f"Duplicate names.*{repeated}"):
        toposort_containers(entries, deps)
