"""`forge.GitHub.check_runs` reads every page of a commit's check-runs (#5977).

GitHub's check-runs endpoint holds 100 runs to a page. A reader of page 1 alone hides a failing
or pending run on page 2, so `decide` could read a partial list as a complete green one. The
same single-page shape hung promote's verify on the combined status (#5974).

Run: uv run pytest scripts/deploy_tools/tests/test_forge_check_run_pages.py
"""

import json
from urllib.parse import parse_qs, urlsplit

import pytest

import forge
from deployer_lib.deploy_git import ci_verdict

T = "a" * 40
FAILING = "lint"


def passing(name: str) -> dict:
    return {"name": name, "status": "completed", "conclusion": "success"}


def two_page_check_runs(
    page_two: list[dict],
    total_count: int | None = None,
    page_one_tail: tuple[dict, ...] = (),
):
    """A check-runs endpoint for T: a full page 1 of passing runs, then `page_two` on page 2.

    Page 1 is filler runs ending in `page_one_tail`, 100 in all. `total_count` defaults to the
    runs the pages hold; a larger one models a short read. A request with no `page` gets
    page 1, as GitHub answers it.
    """
    fillers = 100 - len(page_one_tail)
    page_one = [passing(f"filler-{i:03d}") for i in range(fillers)] + list(
        page_one_tail
    )
    count = len(page_one) + len(page_two) if total_count is None else total_count

    def transport(method, url, headers):
        page = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
        found = {1: page_one, 2: page_two}.get(page, [])
        body = {"total_count": count, "check_runs": found}
        return forge.Response(200, body=json.dumps(body).encode())

    return transport


def test_a_failing_run_on_the_second_page_reaches_the_reader():
    failing = {"name": FAILING, "status": "completed", "conclusion": "failure"}
    runs = forge.GitHub(two_page_check_runs([failing]), None).check_runs(T)
    assert len(runs) == 101
    assert failing in runs


def test_a_failing_run_on_the_second_page_fails_the_ci_verdict():
    failing = {"name": FAILING, "status": "completed", "conclusion": "failure"}
    runs = forge.GitHub(two_page_check_runs([failing]), None).check_runs(T)
    assert ci_verdict(runs, frozenset({FAILING})) == "fail"


@pytest.mark.parametrize(
    ("rerun", "verdict"),
    [
        ({"status": "completed", "conclusion": "failure"}, "fail"),
        ({"status": "in_progress", "conclusion": None}, "pending"),
    ],
    ids=["failing", "pending"],
)
def test_a_rerun_on_the_second_page_overrides_a_pass_on_the_first(rerun, verdict):
    # The required name passes on page 1, so a page-1-only read is a false green.
    transport = two_page_check_runs(
        [{"name": FAILING, **rerun}], page_one_tail=(passing(FAILING),)
    )
    runs = forge.GitHub(transport, None).check_runs(T)
    assert ci_verdict(runs, frozenset({FAILING})) == verdict


def test_a_read_short_of_total_count_is_refused():
    # GitHub reports 150 runs, but page 2 comes back empty: the list read is partial.
    transport = two_page_check_runs([], total_count=150)
    with pytest.raises(forge.ForgeError):
        forge.GitHub(transport, None).check_runs(T)


def test_a_run_created_between_page_reads_is_not_a_short_read():
    # Page 1 reported 101 runs; a re-run started before page 2 was read, which holds two.
    pending = {"name": FAILING, "status": "queued", "conclusion": None}
    transport = two_page_check_runs([passing("late"), pending], total_count=101)
    runs = forge.GitHub(transport, None).check_runs(T)
    assert len(runs) == 102
    assert pending in runs
