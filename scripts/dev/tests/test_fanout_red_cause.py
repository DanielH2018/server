"""Labelling each red test by why it failed on the unchanged code (#4023, slice 2).

The summary lines are pytest's own, measured with `pytest -vv -rA --tb=no` on 2026-10-10.

Run: uv run pytest scripts/dev/tests/test_fanout_red_cause.py
"""

from _review_fakes import PR, _pipeline, _report
from fanout_lib.red_tests import cause, red_by_absence
from fanout_lib.red_gate import Gate, Gates

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


def test_the_reviewer_and_the_record_name_the_red_by_absence_nodes(tmp_path):
    gate = Gate(files=["t.py"], nodes=["t.py::a", "t.py::b"], absent=["t.py::a"])
    reports = [
        _report(structured={"behaviours": []}),
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": []}),
    ]
    pipeline, run = _pipeline(
        tmp_path,
        reports,
        host="daniel-server",
        heads=("base", "red1", "red1"),
        gates=Gates(red=lambda *_: gate, green=lambda *_: ""),
    )
    pipeline.run_all()
    assert pipeline.record.red_by_absence == 1
    review_stdin = run.claude[2][1]
    assert "- `t.py::a`" in review_stdin and "`t.py::b`" not in review_stdin
    assert "1 of them only because a name was missing" in run.comments[0]
