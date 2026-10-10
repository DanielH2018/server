"""Labelling each red test by why it failed on the unchanged code (#4023, slice 2).

The summary lines are pytest's own, measured with `pytest -vv -rA --tb=no` on 2026-10-10.

Run: uv run pytest scripts/dev/tests/test_fanout_red_cause.py
"""

from fanout_lib.red_cause import cause, red_by_absence

SUMMARY = """\
FAILED t.py::test_import - ModuleNotFoundError: No module named 'nosuchmod'
FAILED t.py::test_name - NameError: name 'nosuchname' is not defined
FAILED t.py::test_attr - AttributeError: module 'os' has no attribute 'nosuch'
FAILED t.py::test_cmp - assert 1 == 2
FAILED t.py::test_raise - Failed: DID NOT RAISE ValueError
FAILED t.py::test_boom - AssertionError: boom
FAILED t.py::test_other - RuntimeError: deadlock
"""


def test_a_missing_module_name_or_attribute_is_red_by_absence():
    nodes = ["t.py::test_import", "t.py::test_name", "t.py::test_attr"]
    assert red_by_absence(SUMMARY, nodes) == nodes


def test_an_assertion_or_another_error_is_not_red_by_absence():
    nodes = [
        "t.py::test_cmp",
        "t.py::test_raise",
        "t.py::test_boom",
        "t.py::test_other",
    ]
    assert red_by_absence(SUMMARY, nodes) == []
    assert [
        cause(t) for t in ("assert x", "Failed: DID NOT RAISE E", "OSError: x")
    ] == [
        "assertion",
        "assertion",
        "other",
    ]
