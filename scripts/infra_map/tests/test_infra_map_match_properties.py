"""Workload-to-service matching over drawn service sets where names prefix each other.

A name is a tuple of hyphen-free segments joined by `-`, so the oracle reads ownership as a
tuple prefix and never calls the string matching under test. The property tests run under
the profile `tests/hypothesis_profiles.py` loads.

Run: uv run pytest scripts/infra_map/tests/test_infra_map_match_properties.py
"""

from hypothesis import given
from hypothesis import strategies as st

from infra_map import gen_infra_map as g

NS = "homelab"
_SEGMENT = st.sampled_from(("a", "b", "ab", "0"))
_PATH = st.lists(_SEGMENT, min_size=1, max_size=3).map(tuple)


def _join(path: tuple[str, ...]) -> str:
    return "-".join(path)


@st.composite
def fleets(draw):
    services = draw(st.sets(_PATH, min_size=1, max_size=6))
    workloads = set()
    for service in sorted(services):
        suffixes = draw(st.lists(st.lists(_SEGMENT, max_size=2).map(tuple), max_size=3))
        workloads.update(service + suffix for suffix in suffixes)
    return services, workloads


@given(fleets())
def test_every_workload_goes_to_the_longest_service_it_starts_with(fleet):
    services, workloads = fleet
    names = {_join(s) for s in services}
    live = {
        (NS, _join(w)): {"ready": 1, "desired": 1, "image": "i:1"} for w in workloads
    }
    claims: dict[str, list[str]] = {}
    for name in names:
        for w in g.match_k8s_workloads({"name": name, "namespace": NS}, live, names):
            claims.setdefault(w["name"], []).append(name)
    for workload in workloads:
        owner = max((s for s in services if workload[: len(s)] == s), key=len)
        assert claims[_join(workload)] == [_join(owner)]
