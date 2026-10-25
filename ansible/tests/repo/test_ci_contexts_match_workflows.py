"""await_ci's required CI contexts must match the deployer's gate, and both must name real jobs.

Two drifts, each an outage of its own shape:

A name that no longer matches a `name:` in .github/workflows/ci.yml silently drops a required
check. `ci_verdict` holds the verdict at `pending` for a required name with no runs, so a stale
name does not fail open -- it hangs the wait forever.

A set that differs from `ci_required_contexts` breaks the "agree by construction" claim
await_ci rests on. A name await_ci requires and the deployer does not parks a landing the
deployer would have deployed; the reverse lets a session deploy past a gate the tick would
have held. So the two sets must be EQUAL, not merely overlapping.

Run: uv run pytest ansible/tests/repo/test_ci_contexts_match_workflows.py
"""

import pytest
from lib import yaml_fast
from _helpers import REPO
from lib.repo_paths import ALL_VARS


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


GROUP_VARS = ALL_VARS
DEPLOYER_DEFAULTS = (
    REPO / "ansible" / "roles" / "setup" / "gitops_deploy" / "defaults" / "main.yml"
)


def _contexts_in(path, key: str) -> set[str]:
    """`key`'s list in the YAML file at `path`, asserting it is a literal list."""
    contexts = yaml_fast.safe_load(path.read_text()).get(key)
    assert isinstance(contexts, list), f"{key} is not a literal list in {path.name}"
    return {str(c) for c in contexts}


def _deployer_contexts() -> set[str]:
    """The gate every deploy path grades by, read from the YAML rather than from the rendered
    settings file, which is root-owned and only exists on daniel-box. It is
    `ci_required_contexts` in group_vars/all.yml since H7.2e."""
    return _contexts_in(GROUP_VARS, "ci_required_contexts")


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


def test_the_deployers_role_default_refers_to_the_shared_key():
    """The deployer's settings file renders the role default, so it must name the shared list."""
    default = yaml_fast.safe_load(DEPLOYER_DEFAULTS.read_text())
    assert default["gitops_deploy_ci_contexts"] == "{{ ci_required_contexts }}"
    assert default["gitops_deploy_ci_ancestor_walk_max"] == "{{ ci_ancestor_walk_max }}"


def test_a_reference_in_place_of_the_list_is_flagged():
    """The reject half: reading the role default as a list fails, because it is now a string."""
    with pytest.raises(AssertionError, match="not a literal list"):
        _contexts_in(DEPLOYER_DEFAULTS, "gitops_deploy_ci_contexts")
