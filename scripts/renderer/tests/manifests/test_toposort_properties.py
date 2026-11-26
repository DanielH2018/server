"""`toposort_containers` matches a naive stable sort, or refuses a repeated name.

Names come from a small pool so edges collide and close cycles, and some deps name no
entry. The property tests run under the profile `tests/hypothesis_profiles.py` loads.
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from renderer.template_filters.toposort import DependencyCycle, toposort_containers

_POOL = ("a", "b", "c", "d", "e", "f")
_names = st.sampled_from(_POOL)
# The unique branch keeps most longer lists reaching the order comparison.
_name_lists = st.one_of(st.lists(_names, unique=True), st.lists(_names))
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


@given(names=_name_lists, deps=_deps)
def test_output_is_the_stable_topological_order_or_a_refusal(names, deps):
    entries = [{"name": name} for name in names]
    if repeated := sorted({n for n in names if names.count(n) > 1}):
        with pytest.raises(
            ValueError, match=f"Duplicate names.*: {', '.join(repeated)}$"
        ):
            toposort_containers(entries, deps)
    elif (expected := _naive_order(names, deps)) is None:
        with pytest.raises(DependencyCycle):
            toposort_containers(entries, deps)
    else:
        assert toposort_containers(entries, deps) == [{"name": n} for n in expected]
