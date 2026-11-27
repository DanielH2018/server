"""Properties of `_shared`, the join of sampled rate-limit windows into one per shared quota.

Each input is a window sampled at some time, with resets drawn around one base so that many
fall within `RESET_DRIFT_S` of each other and many tie. The property tests run under the
profile `tests/hypothesis_profiles.py` loads.

Run: uv run pytest hosts/components/gh-quota/tests/test_gh_quota_properties.py
"""

from collections import Counter

from hypothesis import example, given
from hypothesis import strategies as st

from gh_quota import BUCKETS, RESET_DRIFT_S, Window, _shared

BASE = 1_800_000_000

_sessions = st.dictionaries(
    st.sampled_from(["s1", "s2", "s3"]),
    st.builds(
        Counter,
        st.dictionaries(
            st.sampled_from(["pr list", "api read", "run list"]),
            st.integers(1, 5),
            min_size=1,
        ),
    ),
    max_size=3,
)

_sampled_window = st.tuples(
    st.integers(BASE - 3600, BASE).map(float),
    st.builds(
        Window,
        identities=st.sampled_from(["tok-a", "tok-b", "tok-c", "uid-1000"]).map(
            lambda i: (i,)
        ),
        bucket=st.sampled_from(BUCKETS),
        used=st.integers(0, 50),
        quota=st.sampled_from([5000, 15000]),
        reset=st.integers(-2 * RESET_DRIFT_S, 2 * RESET_DRIFT_S).map(
            lambda d: BASE + d
        ),
        sessions=_sessions,
    ),
)

_windows_and_a_permutation = st.lists(_sampled_window, max_size=8).flatmap(
    lambda ws: st.tuples(st.just(ws), st.permutations(ws))
)


def _window(name: str, used: int) -> Window:
    return Window((name,), "core", used, 5000, BASE, {name: Counter({"pr list": 1})})


_A, _B, _C = (
    (1.0, _window("tok-a", 5)),
    (2.0, _window("tok-b", 3)),
    (3.0, _window("tok-c", 7)),
)


def _by_limit(windows: list[Window]) -> dict[tuple[str, int], tuple[Counter, Counter]]:
    totals: dict[tuple[str, int], tuple[Counter, Counter]] = {}
    for w in windows:
        identities, calls = totals.setdefault(
            (w.bucket, w.quota), (Counter(), Counter())
        )
        identities.update(w.identities)
        for session, counted in w.sessions.items():
            calls.update({(session, c): n for c, n in counted.items()})
    return totals


@given(_windows_and_a_permutation)
def test_the_join_keeps_every_identity_and_call_under_its_own_bucket_and_limit(drawn):
    windows, _ = drawn
    assert _by_limit(_shared(windows)) == _by_limit([w for _, w in windows])


@given(_windows_and_a_permutation)
@example(([_A, _B, _C], [_C, _A, _B]))
def test_the_join_does_not_depend_on_the_order_of_its_input(drawn):
    windows, permuted = drawn
    assert _shared(permuted) == _shared(windows)
