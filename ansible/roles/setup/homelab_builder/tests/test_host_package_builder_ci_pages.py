"""The builder's CI gate reads every page of a commit's check runs (#5991).

GitHub pages the check-runs listing at 100 runs. A commit with more runs than that can carry a
failing re-run of a required check on page 2 only, so a gate that reads page 1 alone grades a
red commit green, caches that `pass`, and publishes host packages from it. A read short of the
listing's `total_count` is incomplete, and the builder must read it as `pending`. The fixture
history and the stub render are test_host_package_builder.py's.

Run: uv run pytest ansible/roles/setup/homelab_builder/tests/test_host_package_builder_ci_pages.py
"""

import json
import urllib.parse

from test_host_package_builder import (
    GREEN,
    _Response,
    builder,
    lab,  # noqa: F401 (the fixture, found by name)
    last_built,
)

CONTEXTS = frozenset({"prek"})
PAGE_SIZE = 100
# Runs of checks the gate does not require, enough to fill page 1 on their own.
FILLER = [
    {"name": f"matrix ({i})", "status": "completed", "conclusion": "success"}
    for i in range(PAGE_SIZE - 1)
]
PREK_PASS = {"name": "prek", "status": "completed", "conclusion": "success"}
PREK_FAIL = {"name": "prek", "status": "completed", "conclusion": "failure"}


class PagedGitHub:
    """`urlopen` for the check-runs API that pages each SHA's runs as GitHub does.

    `runs[sha]` is the full listing. `total[sha]` overrides the `total_count` the body reports,
    for a listing that shrinks between two page reads.
    """

    def __init__(self):
        self.runs: dict[str, list[dict]] = {}
        self.total: dict[str, int] = {}

    def __call__(self, request, timeout=None):
        url = urllib.parse.urlsplit(request.full_url)
        sha = url.path.split("/commits/")[1].split("/")[0]
        query = urllib.parse.parse_qs(url.query)
        per_page = min(int(query.get("per_page", ["30"])[0]), PAGE_SIZE)
        page = int(query.get("page", ["1"])[0])
        runs = self.runs.get(sha, [])
        body = {
            "total_count": self.total.get(sha, len(runs)),
            "check_runs": runs[(page - 1) * per_page : page * per_page],
        }
        return _Response(json.dumps(body).encode())


def paged_builder(lab):  # noqa: F811
    origin, state, etc, _ = lab
    github = PagedGitHub()
    return builder((origin, state, etc, github)), github


def armed(run):
    """The state `main` sets before it grades a commit, for a direct `verdict` call."""
    run.config = {"GITHUB_REPO": "owner/name"}
    run.cache = {}
    run.token = "t0ken"
    return run


def test_a_failing_rerun_only_on_page_two_grades_fail(lab):  # noqa: F811
    origin, *_ = lab
    sha = origin.commit("a")
    run, github = paged_builder(lab)
    github.runs[sha] = [*FILLER, PREK_PASS, PREK_FAIL]
    run = armed(run)
    assert run.verdict(sha, CONTEXTS) == "fail"
    assert sha not in run.cache, "a red commit was cached as pass"


def test_a_read_short_of_total_count_grades_pending(lab):  # noqa: F811
    origin, *_ = lab
    sha = origin.commit("a")
    run, github = paged_builder(lab)
    # Page 1 is full and green; page 2 comes back empty while total_count says 150.
    github.runs[sha] = [*FILLER, PREK_PASS]
    github.total[sha] = 150
    run = armed(run)
    assert run.verdict(sha, CONTEXTS) == "pending"
    assert sha not in run.cache, "an incomplete read was cached"


def test_a_required_check_only_on_page_two_is_read(lab):  # noqa: F811
    origin, *_ = lab
    sha = origin.commit("a")
    run, github = paged_builder(lab)
    github.runs[sha] = [
        *FILLER,
        {"name": "lint", "status": "completed", "conclusion": "success"},
        PREK_PASS,
    ]
    run = armed(run)
    assert run.verdict(sha, CONTEXTS) == "pass"


def test_a_tip_red_on_page_two_is_never_built(lab):  # noqa: F811
    origin, state, *_ = lab
    parent = origin.commit("a")
    tip = origin.commit("b")
    run, github = paged_builder(lab)
    github.runs = {parent: GREEN, tip: [*FILLER, PREK_PASS, PREK_FAIL]}
    assert run.main() == 0
    assert last_built(state) == parent, "packages were built from a red commit"
    cache = json.loads((state / "ci-cache.json").read_text())
    assert tip not in cache
