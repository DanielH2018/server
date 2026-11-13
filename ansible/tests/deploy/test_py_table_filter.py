"""Unit tests for `py_table` in renderer/template_filters/py_table.py.

monitor-bridge's env-secret reads the bridge's check table through this filter (#3659, #3781).
A row the filter drops is a check missing from the rendered Secret, so each way it can lose a
row raises instead.

Run: uv run pytest ansible/tests/deploy/test_py_table_filter.py
"""

import pytest

from renderer.template_filters.py_table import py_table

SOURCE = """
from checks import service

ROWS: tuple[Row, ...] = (
    Row(name="n8n", fn=service.check_n8n, critical=True, display="n8" "n"),
    Row(name="bazarr", fn=service.check_bazarr, runbook=None),
)
"""


def test_literal_keywords_are_read_and_code_is_left_out():
    assert py_table(SOURCE, "ROWS") == [
        {"name": "n8n", "critical": True, "display": "n8n"},
        {"name": "bazarr", "runbook": None},
    ]


def test_a_plain_assignment_is_read_too():
    assert py_table("T = [Row(a=1)]\n", "T") == [{"a": 1}]


def test_a_missing_table_is_flagged():
    with pytest.raises(ValueError, match="no tuple or list is assigned to OTHER"):
        py_table(SOURCE, "OTHER")


def test_a_positional_argument_is_flagged():
    with pytest.raises(ValueError, match=r"T\[0\] passes a positional argument"):
        py_table('T = (Row("disk"),)\n', "T")


def test_an_element_that_is_not_a_call_is_flagged():
    with pytest.raises(ValueError, match=r"T\[1\] is not a call"):
        py_table("T = (Row(a=1), {'a': 2})\n", "T")


# ── error shape (#4505) ─────────────────────────────────────────────────────────────────────
