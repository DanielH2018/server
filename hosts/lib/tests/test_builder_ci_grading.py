"""The builder's CI grading and its metric reasons, held to `hosts/lib/ci_verdict.py` and the plan's.

The builder grades with `ci_verdict.ci_verdict`, which its component ships beside it, so a commit
reads the same verdict there as in `homelab-checkout-sync`.

Run: uv run pytest hosts/lib/tests/test_builder_ci_grading.py
"""

import host_package_builder as hb
from ci_verdict import (
    CI_GATE_NAME,
    CI_GATE_NEEDS_JOBS,
    CI_PROVE_JOB,
    ci_verdict,
    parse_check_runs,
)
from test_host_package_builder import lab  # noqa: F401 (the fixture, found by name)
from test_host_package_builder_ci_pages import armed, paged_builder


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


def test_a_gate_whose_jobs_all_passed_grades_pass_as_checkout_sync_grades_it(lab):  # noqa: F811
    origin, *_ = lab
    sha = origin.commit("a")
    run, github = paged_builder(lab)
    jobs = [
        {"name": name, "status": "completed", "conclusion": "success"}
        for name in (CI_PROVE_JOB, *CI_GATE_NEEDS_JOBS)
    ]
    github.runs[sha] = jobs
    gate = frozenset({CI_GATE_NAME})

    assert ci_verdict(parse_check_runs(jobs), gate) == "pass"
    assert armed(run).verdict(sha, gate) == "pass"
