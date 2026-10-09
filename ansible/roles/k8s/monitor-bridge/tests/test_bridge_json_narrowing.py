"""A fetched body of the wrong shape fails where it is read, as a RuntimeError the runner pages on."""

import bridge.net
import pytest
from bridge.types import as_object, as_object_list, optional_object


def test_a_query_body_that_is_not_an_object_raises():
    with pytest.raises(
        RuntimeError, match=r"prometheus response: expected a JSON object"
    ):
        bridge.net._query_result([], "prometheus")


def test_a_non_success_status_keeps_its_message():
    with pytest.raises(RuntimeError, match=r"loki query status=error"):
        bridge.net._query_result({"status": "error"}, "loki")


def test_a_labels_status_names_the_request():
    with pytest.raises(RuntimeError, match=r"loki labels status=error"):
        bridge.net._query_result({"status": "error"}, "loki", "labels")


def test_a_sample_whose_value_is_not_a_number_raises():
    with pytest.raises(RuntimeError, match=r"series value is NoneType, not a number"):
        bridge.net._sample_value({"metric": {}, "value": [0, None]})


def test_a_sample_value_is_read_from_prometheus_strings():
    assert bridge.net._sample_value({"value": [0, "1.5"]}) == 1.5


def test_labels_keep_only_string_values():
    assert bridge.net._labels({"metric": {"job": "node", "n": 1}}) == {"job": "node"}


def test_as_object_list_names_the_offending_entry():
    with pytest.raises(
        RuntimeError, match=r"indexers\[1\]: expected a JSON object, got int"
    ):
        as_object_list([{}, 7], "indexers")


def test_optional_object_passes_null_and_rejects_a_list():
    assert optional_object(None, "state") is None
    with pytest.raises(RuntimeError, match=r"state: expected a JSON object, got list"):
        optional_object([], "state")
    assert as_object({"a": 1}, "state") == {"a": 1}
