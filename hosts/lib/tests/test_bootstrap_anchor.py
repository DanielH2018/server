"""`hosts/lib/bootstrap_anchor.py`: a clone's HEAD anchors only on master's first-parent chain
with a green CI verdict. The GitHub reads are stubbed; `judge` is the whole decision.

Run: uv run pytest hosts/lib/tests/test_bootstrap_anchor.py
"""

import io
import json

import bootstrap_anchor as anchor
import pytest

CONTEXTS = frozenset({"prek"})
GREEN = [{"name": "prek", "status": "completed", "conclusion": "success"}]
RED = [{"name": "prek", "status": "completed", "conclusion": "failure"}]


def refusal(sha, chain, runs, contexts) -> str:
    problem = anchor.judge(sha, chain, runs, contexts)
    assert problem is not None
    return problem


def test_a_green_first_parent_commit_anchors():
    assert anchor.judge("abc", ["abc", "def"], lambda: GREEN, CONTEXTS) is None


def test_a_commit_off_the_first_parent_chain_is_refused_naming_the_half():
    problem = refusal("zzz", ["abc"], lambda: GREEN, CONTEXTS)

    assert problem.startswith("first-parent:")


def test_a_red_verdict_is_refused_naming_the_half():
    assert refusal("abc", ["abc"], lambda: RED, CONTEXTS).startswith("ci:")


def test_an_unreadable_verdict_is_refused_not_passed():
    def unreadable():
        raise OSError("no route")

    assert "unreadable" in refusal("abc", ["abc"], unreadable, CONTEXTS)


def test_an_empty_context_set_is_refused_because_it_would_grade_everything_green():
    assert refusal("abc", ["abc"], lambda: GREEN, frozenset()).startswith("ci:")


def _opener(pages):
    served = iter(pages)

    def open_page(request, timeout):
        return io.StringIO(json.dumps(next(served)))

    return open_page


def test_check_runs_walks_every_page():
    full = [{"name": str(n)} for n in range(anchor.PAGE_SIZE)]
    pages = [
        {"total_count": 101, "check_runs": full},
        {"total_count": 101, "check_runs": [{"name": "x"}]},
    ]

    assert len(anchor.check_runs("o/n", "abc", _opener(pages))) == 101


def test_a_check_runs_read_short_of_the_total_raises():
    short = [{"total_count": 150, "check_runs": [{"name": "x"}]}]

    with pytest.raises(ValueError, match="1 of 150"):
        anchor.check_runs("o/n", "abc", _opener(short))
