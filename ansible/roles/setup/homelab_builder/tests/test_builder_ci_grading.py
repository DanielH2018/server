"""The builder's CI grading and its metric reasons, held to the deployer's and the plan's.

The builder cannot import `deploy_git`, so `builder_ci_grade.py` carries its own copy of `ci_verdict`, the
two conclusion sets and `read_check_pages`. These tests hold the copy equal to the deployer's.

Run: uv run pytest ansible/roles/setup/homelab_builder/tests/test_builder_ci_grading.py
"""

import builder_ci_grade as cg
import host_package_builder as hb
import pytest
from deployer_lib import deploy_git

GREEN = [{"name": "prek", "status": "completed", "conclusion": "success"}]
RED = [{"name": "prek", "status": "completed", "conclusion": "failure"}]
PENDING = [{"name": "prek", "status": "in_progress", "conclusion": None}]


def test_every_reason_is_a_plan_value_and_none_is_trust():
    assert "trust" not in hb.REASONS
    assert hb.REASONS <= {
        "hold",
        "ancestry",
        "keyring",
        "ci",
        "no-green",
        "token",
        "tools",
        "canary",
        "ok",
        "build",
    }


@pytest.mark.parametrize(
    "runs",
    [
        GREEN,
        RED,
        PENDING,
        [],
        GREEN + RED,
        [{"name": "prek", "status": "completed", "conclusion": "cancelled"}],
        [{"name": "other", "status": "completed", "conclusion": "failure"}],
    ],
)
def test_the_grading_matches_the_deployers(runs):
    assert cg.ci_verdict(runs, frozenset({"prek"})) == deploy_git.ci_verdict(
        runs, frozenset({"prek"})
    )


def test_the_conclusion_sets_match_the_deployers():
    assert cg._CI_PASS_CONCLUSIONS == deploy_git._CI_PASS_CONCLUSIONS
    assert cg._CI_NO_VERDICT_CONCLUSIONS == deploy_git._CI_NO_VERDICT_CONCLUSIONS


def test_the_page_size_and_cap_match_the_deployers():
    assert (cg._CHECK_PAGE_SIZE, cg._CHECK_PAGES_MAX) == (
        deploy_git.CHECK_PAGE_SIZE,
        deploy_git.CHECK_PAGES_MAX,
    )


def _listing(runs: int, total: int | None = None):
    """A `fetch_page` over `runs` filler runs whose body reports `total` (default: `runs`)."""
    listing = [{"name": f"r{i}"} for i in range(runs)]

    def fetch_page(query: str) -> dict:
        page = int(query.rsplit("page=", 1)[1])
        size = cg._CHECK_PAGE_SIZE
        return {
            "total_count": runs if total is None else total,
            "check_runs": listing[(page - 1) * size : page * size],
        }

    return fetch_page


@pytest.mark.parametrize(
    "fetch_page",
    [
        _listing(0),
        _listing(100),
        _listing(150),
        _listing(100, total=150),
        _listing(150, total=120),
        _listing(2100),
        lambda query: {"check_runs": []},
        lambda query: {"total_count": 0},
        lambda query: [],
    ],
    ids=[
        "empty",
        "one-full-page",
        "two-pages",
        "short-of-total",
        "past-total",
        "past-the-cap",
        "no-total",
        "no-list",
        "not-a-dict",
    ],
)
def test_the_page_walk_matches_the_deployers(fetch_page):
    def outcome(read):
        try:
            return read(fetch_page, "check_runs")
        except ValueError as exc:
            return f"ValueError: {exc}"

    assert outcome(cg.read_check_pages) == outcome(deploy_git.read_check_pages)
