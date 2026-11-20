import pytest

from flux_job_parse import EndpointState, Series, endpoint_states, parse_prom_vector


@pytest.mark.parametrize(
    ("conditions", "state"),
    [
        ({"ready": True}, EndpointState.READY),
        ({"ready": False}, EndpointState.NOT_READY),
        ({}, EndpointState.UNSTATED),
        (None, EndpointState.UNSTATED),
        ({"ready": False, "terminating": True}, EndpointState.TERMINATING),
        ({"ready": True, "terminating": True}, EndpointState.TERMINATING),
        ({"terminating": True}, EndpointState.TERMINATING),
        ({"ready": True, "terminating": False}, EndpointState.READY),
    ],
)
def test_an_endpoints_conditions_decide_its_state(conditions, state):
    assert EndpointState.of({"conditions": conditions}) == state


def test_a_slice_with_null_endpoints_contributes_no_state():
    items = [{"endpoints": None}, {"endpoints": [{"conditions": {"ready": True}}]}]
    assert endpoint_states(items) == [EndpointState.READY]


def test_a_vector_answer_parses_into_labels_and_values():
    body = {
        "status": "success",
        "data": {
            "result": [
                {"metric": {"status": "403", "le": 1}, "value": [1700000000, "2"]},
                {"value": [1700000000, "NaN"]},
            ]
        },
    }
    first, second = parse_prom_vector(body)
    assert first == Series({"status": "403"}, 2.0)
    assert second.labels == {} and second.value != second.value


def test_an_absent_data_or_result_reads_as_no_samples():
    assert parse_prom_vector({"status": "success"}) == []
    assert parse_prom_vector({"status": "success", "data": {}}) == []


@pytest.mark.parametrize(
    "body",
    [
        {"status": "success", "data": None},
        {"status": "success", "data": {"result": None}},
        {"status": "error", "data": {"result": []}},
        ["not", "an", "object"],
        {"status": "success", "data": {"result": [None]}},
        {"status": "success", "data": {"result": [{"metric": {}, "value": [1]}]}},
        {"status": "success", "data": {"result": [{"metric": {}, "value": [1, "x"]}]}},
    ],
    ids=[
        "null-data",
        "null-result",
        "error-status",
        "not-an-object",
        "null-sample",
        "short-value",
        "non-numeric-value",
    ],
)
def test_an_answer_that_is_not_an_instant_vector_raises(body):
    with pytest.raises(ValueError, match=r"answer|sample"):
        parse_prom_vector(body)
