"""The narrowing helpers a caller applies to a parsed JSON document."""

import pytest
from json_types import as_list, as_object, as_object_list


def test_as_object_names_what_it_received():
    with pytest.raises(
        ValueError, match=r"gh pr view: expected a JSON object, got list"
    ):
        as_object([], "gh pr view")


def test_as_list_rejects_an_object():
    with pytest.raises(ValueError, match=r"x: expected a JSON array, got dict"):
        as_list({}, "x")


def test_as_object_list_reads_none_as_empty():
    assert as_object_list(None, "gh issue list") == []


def test_as_object_list_names_the_offending_entry():
    with pytest.raises(
        ValueError, match=r"issues\[1\]: expected a JSON object, got str"
    ):
        as_object_list([{"n": 1}, "oops"], "issues")
