import pytest
from dev.sweep_report import TITLE, report

REPO = "o/r"
RUN = "https://github.com/o/r/actions/runs/1"


def _report(result: str, issues: list[dict], modules=("tests/a/test_x.py",)):
    calls = []
    report(
        REPO,
        result,
        RUN,
        list(modules),
        run=lambda *args: calls.append(args),
        read=lambda *args: issues,
    )
    return calls


def test_the_first_red_sweep_opens_an_issue_naming_the_failed_modules():
    calls = _report("failure", [])
    assert [c[:2] for c in calls] == [("issue", "create")]
    body = calls[0][calls[0].index("--body") + 1]
    assert "`tests/a/test_x.py`" in body and RUN in body


def test_a_sweep_that_stays_red_comments_on_the_open_issue():
    calls = _report("failure", [{"number": 7, "title": TITLE}])
    assert [c[:3] for c in calls] == [("issue", "comment", "7")]


def test_a_green_sweep_closes_the_open_issue():
    calls = _report("success", [{"number": 7, "title": TITLE}])
    assert [c[:3] for c in calls] == [("issue", "close", "7")]


def test_an_issue_whose_title_only_contains_the_words_is_not_the_sweep_issue():
    calls = _report("success", [{"number": 9, "title": f"Re: {TITLE} again"}])
    assert calls == []


@pytest.mark.parametrize("result", ["cancelled", "skipped"])
def test_a_sweep_with_no_verdict_changes_nothing(result):
    calls = _report(result, [{"number": 7, "title": TITLE}])
    assert calls == []
