"""The standalone-value guard's resolver (target slice R2, PR 1c).

The values and their outcomes were measured against the pinned flux CLI v2.9.6; CI's flux
probe re-measures the corpus on every run.

Run: uv run pytest scripts/lib/tests/test_plain_scalar.py
"""

import pytest

from lib.plain_scalar import go_yaml_v2_types, refused, yaml_1_1_keeps


@pytest.mark.parametrize(
    "text", ["0123", "true", "yes", "1e3", "null", "", "1:20", "2001-12-14"]
)
def test_a_value_flux_would_retype_is_flagged(text):
    assert refused(text)


@pytest.mark.parametrize("text", ["abc", "a.b-c.example", "0123a"])
def test_a_value_flux_keeps_as_the_same_string_is_clean(text):
    assert not refused(text)


@pytest.mark.parametrize(
    "text",
    # PyYAML reads each as a string; the pinned CLI returned 1000, 8, 15, 1000 and true.
    ["1e3", "08", "0o17", "1.e3", "y"],
)
def test_the_go_yaml_port_types_what_yaml_1_1_reads_as_a_string(text):
    assert yaml_1_1_keeps(text)
    assert go_yaml_v2_types(text)


@pytest.mark.parametrize(
    "text",
    # The pinned CLI returned `#x` as null, ` x` as `x` and `'a'` as `a`, and failed to build
    # `a: b`. go-yaml's resolver sees none of them, because each is a syntax change.
    ["#x", " x", "'a'", "a: b"],
)
def test_text_that_is_no_plain_scalar_is_flagged_by_the_yaml_1_1_half(text):
    assert not go_yaml_v2_types(text)
    assert refused(text)
