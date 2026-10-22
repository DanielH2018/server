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


def two_page_check_runs(page_two: list[dict], total_count: int | None = None):
    """A check-runs endpoint for T: 100 passing filler runs, then `page_two` on page 2.

    `total_count` defaults to the runs the pages hold; a larger one models a short read. A
    request with no `page` gets page 1, as GitHub answers it.
    """
    page_one = [passing(f"filler-{i:03d}") for i in range(100)]
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


def test_a_read_short_of_total_count_is_refused():
    # GitHub reports 150 runs, but page 2 comes back empty: the list read is partial.
    transport = two_page_check_runs([], total_count=150)
    with pytest.raises(forge.ForgeError):
        forge.GitHub(transport, None).check_runs(T)
