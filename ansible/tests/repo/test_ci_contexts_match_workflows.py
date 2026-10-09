"""await_ci's required CI contexts must match the deployer's gate, and both must name real jobs.

Two drifts, each an outage of its own shape:

A name that no longer matches a `name:` in .github/workflows/ci.yml silently drops a required
check. `ci_verdict` holds the verdict at `pending` for a required name with no runs, so a stale
name does not fail open -- it hangs the wait forever.

A set that differs from `gitops_deploy_ci_contexts` breaks the "agree by construction" claim
await_ci rests on. A name await_ci requires and the deployer does not parks a landing the
deployer would have deployed; the reverse lets a session deploy past a gate the tick would
have held. So the two sets must be EQUAL, not merely overlapping.

Run: uv run pytest ansible/tests/repo/test_ci_contexts_match_workflows.py
"""

from lib import yaml_fast
from _helpers import REPO


import await_ci


def _workflow_job_names() -> set[str]:
    """The check-run name of every job in ci.yml: its `name:`, or its id when it has none.

    Read from the parsed workflow, so a step's `name:` can never enter the set however the
    file is indented (#3663).
    """
    jobs = yaml_fast.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text())[
        "jobs"
    ]
    return {str(job.get("name", job_id)) for job_id, job in jobs.items()}


def _deployer_contexts() -> set[str]:
    """The deployer's own gate, read from the YAML defaults rather than from config.env --
    that file is rendered, root-owned, and only exists on daniel-box."""
    contexts = yaml_fast.safe_load(
        (
            REPO
            / "ansible"
            / "roles"
            / "setup"
            / "gitops_deploy"
            / "defaults"
            / "main.yml"
        ).read_text()
    ).get("gitops_deploy_ci_contexts")
    assert isinstance(contexts, list), (
        "gitops_deploy_ci_contexts is not a literal list in defaults/main.yml"
    )
    return {str(c) for c in contexts}


def test_await_ci_requires_exactly_what_the_deployer_requires():
    assert await_ci.required_contexts() == _deployer_contexts()


def test_the_deployer_context_parse_actually_finds_names():
    """A regex that matched nothing would make the equality test compare two empty sets --
    and an empty required set is the disarmed gate await_ci refuses to report against."""
    assert _deployer_contexts()


def test_every_required_context_names_a_real_job():
    missing = await_ci.required_contexts() - _workflow_job_names()
    assert not missing, f"required contexts naming no ci.yml job: {sorted(missing)}"


def test_a_bogus_context_would_be_flagged():
    """The reject half.

    Without this, a parser that returned every string in the file would pass the check above while
    proving nothing.
    """
    assert "not-a-real-job" not in _workflow_job_names()


def test_the_workflow_parse_actually_finds_jobs():
    """A regex that matched nothing would make the coverage test vacuously true."""
    assert _workflow_job_names(), "parsed no job names out of ci.yml"
