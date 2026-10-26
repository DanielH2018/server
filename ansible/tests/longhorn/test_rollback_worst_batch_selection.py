"""Red proof for the worst-batch selection in `test_rollback_timeout_budget._worst_batch`.

`_worst_batch(promoted, cap, per_claim, stabilise, in_role_s, rollout_s)` returns the `cap`
most expensive services in `promoted` and their rollback cost. A batch no longer than `cap`,
or one whose costs tie, keeps every candidate however the selection sorts, so it cannot tell
the right batch from the cheapest. The fixture below is longer than either cap and its
costs are distinct.

The fixture orders each wrong key differently from the whole per-service cost. At
per_claim=100 each role costs claims * 100 + in-role wait + rollout wait:

    alpha    2 claims, in-role   0, rollout 100  ->  300  (most claims, cheapest)
    bravo    1 claim,  in-role   0, rollout 300  ->  400
    charlie  1 claim,  in-role 400, rollout 100  ->  600  (first only once in-role counts)

`reverse=False`, a claims-only key and an unsorted slice each pick alpha first, and a key
that drops the in-role wait picks bravo first.
"""

import importlib

_PER_CLAIM = 100
_STABILISE = 60
_IN_ROLE = {"alpha": 0, "bravo": 0, "charlie": 400}
_ROLLOUT = {"alpha": 100, "bravo": 300, "charlie": 100}
_PROMOTED = [("alpha", 2), ("bravo", 1), ("charlie", 1)]


def _select(cap: int) -> list[str]:
    name = "_worst_batch"
    worst_batch = getattr(
        importlib.import_module("test_rollback_timeout_budget"), name, None
    )
    assert callable(worst_batch), (
        "test_rollback_timeout_budget has no _worst_batch(promoted, cap, per_claim, "
        "stabilise, in_role_s, rollout_s) to select the worst batch with"
    )
    batch, _ = worst_batch(
        _PROMOTED, cap, _PER_CLAIM, _STABILISE, _IN_ROLE.get, _ROLLOUT.get
    )
    return sorted(batch)


def test_cap_one_selects_the_single_most_expensive_role():
    assert _select(1) == ["charlie"]


def test_cap_two_selects_the_two_most_expensive_roles():
    assert _select(2) == ["bravo", "charlie"]
