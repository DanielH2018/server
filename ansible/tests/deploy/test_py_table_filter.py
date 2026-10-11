"""Unit tests for `py_table` in filter_plugins/py_table.py.

monitor-bridge's env-secret and uptime-kuma's static monitors both read the bridge's check
table through this filter (#3659, #3781). A row the filter drops is a check pushing nowhere and
a tile AutoKuma deletes, so each way it can lose a row raises instead.

Lives in ansible/tests/ (not under filter_plugins/) so Ansible's filter-plugin loader doesn't
import it as a plugin.

Run: uv run pytest ansible/tests/deploy/test_py_table_filter.py
"""

import pytest

from ansible.errors import AnsibleFilterError
from py_table import FilterModule, py_table

SOURCE = """
from checks import host

ROWS: tuple[Row, ...] = (
    Row(name="disk", fn=host.check_disk, critical=True, display="Root" " Disk"),
    Row(name="cert", fn=host.check_cert, runbook=None),
)
"""


def test_literal_keywords_are_read_and_code_is_left_out():
    assert py_table(SOURCE, "ROWS") == [
        {"name": "disk", "critical": True, "display": "Root Disk"},
        {"name": "cert", "runbook": None},
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


@pytest.mark.parametrize(
    "source",
    ["ROWS = 1\n", "ROWS = (1,)\n", "ROWS = (Row(1),)\n"],
    ids=["no-table", "not-a-call", "positional"],
)
def test_a_refusal_is_an_ansible_filter_error_named_for_its_filter(source):
    """A render failure names the filter that refused, through the filter Ansible calls."""
    with pytest.raises((ValueError, AnsibleFilterError)) as caught:
        FilterModule().filters()["py_table"](source, "ROWS")
    assert isinstance(caught.value, AnsibleFilterError), type(caught.value)
    assert str(caught.value).startswith("py_table: "), str(caught.value)
