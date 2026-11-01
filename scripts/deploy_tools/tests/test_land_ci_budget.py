"""land.sh's master CI budget covers the longest `timeout-minutes` chain into the gate (#6857).

`await_ci` waits on the `prek` gate alone, and the gate starts only once every job it
`needs` has ended. So the longest chain of job timeouts ending at the gate bounds how long
master CI can take and still reach a verdict. A budget shorter than that chain ends a landing
on `ci-timeout` while CI is still able to go green, as it did for every host-package change.

Run: uv run pytest scripts/deploy_tools/tests/test_land_ci_budget.py
"""

from deploy_tools.land_lib import options
from lib import yaml_fast
from lib.repo_paths import REPO

GATE = "prek"
# GitHub's own job timeout when `timeout-minutes` is unset.
GITHUB_DEFAULT_MINUTES = 360
QUEUE_MARGIN_S = 600


def chain_into(jobs: dict, job: str) -> tuple[int, list[str]]:
    """The longest sum of `timeout-minutes` over a `needs` path ending at `job`, and the path."""
    needs = jobs[job].get("needs", [])
    needs = [needs] if isinstance(needs, str) else needs
    longest = max((chain_into(jobs, n) for n in needs), default=(0, []))
    minutes = jobs[job].get("timeout-minutes", GITHUB_DEFAULT_MINUTES)
    return longest[0] + minutes, [*longest[1], job]


def budget_problem(jobs: dict, budget_s: int) -> str | None:
    """Why `budget_s` can time out before the gate reaches a verdict, or None."""
    minutes, path = chain_into(jobs, GATE)
    needed = minutes * 60 + QUEUE_MARGIN_S
    if budget_s < needed:
        return f"{budget_s}s < {needed}s for {' > '.join(path)}"
    return None


def jobs(build: int, gate: int | None = 5) -> dict:
    gate_job: dict = {"needs": ["build"]}
    if gate is not None:
        gate_job["timeout-minutes"] = gate
    return {"build": {"timeout-minutes": build}, GATE: gate_job}


def test_a_budget_over_the_chain_is_clean():
    assert budget_problem(jobs(build=30), 35 * 60 + QUEUE_MARGIN_S) is None


def test_a_budget_under_the_chain_is_flagged():
    assert budget_problem(jobs(build=30), 20 * 60) == "1200s < 2700s for build > prek"


def test_a_gate_with_no_timeout_is_flagged():
    assert budget_problem(jobs(build=30, gate=None), 3600) is not None


def test_land_shs_budget_covers_ci_ymls_chain_into_the_gate():
    workflow = yaml_fast.safe_load((REPO / ".github/workflows/ci.yml").read_text())
    _minutes, path = chain_into(workflow["jobs"], GATE)
    assert "host_packages" in path, path
    assert budget_problem(workflow["jobs"], options.Options.ci_timeout) is None
