"""The one Longhorn robustness allow-list, and the two callers that must share it (#3668)."""

import longhorn_robustness
from deploy_tools.runbook_gates_lib import gate_runner


def test_an_unrecognised_or_missing_state_is_unsafe():
    assert longhorn_robustness.unsafe_volumes(
        [("a", "healthy"), ("b", "unknown"), ("c", "rebuilding"), ("d", "")]
    ) == {"c": "rebuilding", "d": ""}


def test_the_worst_state_wins_whichever_order_it_arrives_in():
    rows = [("v", "degraded"), ("v", "faulted"), ("v", "rebuilding"), ("v", "healthy")]
    assert longhorn_robustness.unsafe_volumes(rows) == {"v": "faulted"}
    assert longhorn_robustness.unsafe_volumes(rows[::-1]) == {"v": "faulted"}
    assert longhorn_robustness.unsafe_volumes(
        [("v", "degraded"), ("v", "rebuilding")]
    ) == {"v": "rebuilding"}


def test_the_runbook_gates_call_the_one_function():
    # Identity, not equal output: a second copy that agrees today is the duplication #3668
    # removed, and it would drift the next time one side changed. The `LonghornVolumeDegraded`
    # rule is the other reader since M6 batch A5; it is PromQL, so
    # ansible/tests/services/test_storage_rule_literals.py holds its `state!~` regex to this
    # module's SAFE_ROBUSTNESS instead.
    assert gate_runner._unsafe_states is longhorn_robustness.unsafe_volumes
