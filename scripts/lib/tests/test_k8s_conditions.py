import pytest

from lib.k8s_conditions import Condition, ConditionStatus, parse_conditions


def test_conditions_are_keyed_by_type_and_the_first_of_a_type_wins():
    item = {
        "status": {
            "conditions": [
                {
                    "type": "Ready",
                    "status": "True",
                    "reason": "Done",
                    "message": "ok",
                    "lastTransitionTime": "2026-11-20T10:00:00Z",
                    "observedGeneration": 3,
                },
                {"type": "Ready", "status": "False", "message": "later"},
                None,
                {"type": "Stalled", "status": "maybe", "observedGeneration": "3"},
            ]
        }
    }
    assert parse_conditions(item) == {
        "Ready": Condition(
            ConditionStatus.TRUE, "Done", "ok", "2026-11-20T10:00:00Z", 3
        ),
        "Stalled": Condition(ConditionStatus.UNKNOWN, "", "", "", None),
    }


@pytest.mark.parametrize(
    "item",
    [
        {"status": "Ready"},
        {"status": ["Ready"]},
        {"status": {"conditions": {"type": "Ready", "status": "True"}}},
        {"status": {"conditions": "Ready"}},
    ],
)
def test_a_malformed_status_reads_as_no_conditions(item):
    assert parse_conditions(item) == {}
