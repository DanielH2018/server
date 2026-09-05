"""Every job in ci.yml must be gated by a required status context.

`test_ci_contexts_match_workflows.py` checks one direction: every required context names a real
job. Nothing checked the reverse. A fourth job added to ci.yml runs advisory-only unless it is
either a dependency of the `prek` gate job or itself named in the merge gate, and a job whose
failure cannot block a merge is a check nobody reads.

The gate is reachable two ways, so both count as gated:

- `prek` (`needs: [hooks, pytest]`) re-reports its dependencies under the one required context
  name, so a job in that `needs` list blocks a merge through it.
- A job whose display name is in `gitops_deploy_expected_ruleset_contexts` is required by the
  ruleset directly, as `renovate config validator` is.

The context list is read from `roles/setup/gitops_deploy/defaults/main.yml`, the repo's committed
copy of ruleset 20912512. `github-ruleset-drift.sh` compares that copy against the live ruleset
daily, so this guard needs no network and still tracks the real gate.

Run: uv run pytest ansible/tests/repo/test_every_ci_job_is_gated.py
"""

from __future__ import annotations

import re

from _helpers import REPO
from lib import yaml_fast

CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"

# The job that re-reports the required context for the halves it depends on.
GATE_JOB = "prek"

# Jobs ci.yml is known to declare. A parser that returned an empty mapping would make the subset
# check below vacuously true, so a rename fails here rather than silently shrinking coverage.
KNOWN_JOBS = frozenset({"hooks", "pytest", GATE_JOB, "renovate-config"})


def ungated_jobs(workflow_text: str, required_contexts: set[str]) -> set[str]:
    """Job ids that neither feed the gate job nor carry a required context name."""
    jobs = yaml_fast.safe_load(workflow_text)["jobs"]
    needs = jobs.get(GATE_JOB, {}).get("needs") or []
    if isinstance(needs, str):
        needs = [needs]
    return {
        job_id
        for job_id, spec in jobs.items()
        if job_id not in set(needs)
        and (spec.get("name") or job_id) not in required_contexts
    }


def _ruleset_contexts() -> set[str]:
    """The merge gate, read from the YAML defaults as a literal list -- the same regex shape the
    sibling guard uses for `gitops_deploy_ci_contexts`."""
    text = (
        REPO / "ansible" / "roles" / "setup" / "gitops_deploy" / "defaults" / "main.yml"
    ).read_text()
    block = re.search(
        r"^gitops_deploy_expected_ruleset_contexts:\s*\n((?:\s*-\s.+\n)+)", text, re.M
    )
    assert block, "gitops_deploy_expected_ruleset_contexts is not a literal list"
    return {line.strip().lstrip("-").strip() for line in block.group(1).splitlines()}


def test_every_ci_job_is_gated_by_a_required_context():
    ungated = ungated_jobs(CI_WORKFLOW.read_text(), _ruleset_contexts())
    assert not ungated, (
        f"ci.yml jobs that block no merge: {sorted(ungated)}. Add each to the `prek` job's "
        "`needs`, or make it a required context in the ruleset and record it in "
        "gitops_deploy_expected_ruleset_contexts."
    )


def test_the_workflow_parse_finds_the_jobs_it_must_find():
    """Non-vacuity. An empty jobs mapping passes the check above while covering nothing."""
    jobs = set(yaml_fast.safe_load(CI_WORKFLOW.read_text())["jobs"])
    assert KNOWN_JOBS <= jobs, f"ci.yml no longer declares {sorted(KNOWN_JOBS - jobs)}"


def test_the_context_parse_finds_the_gate_jobs_name():
    contexts = _ruleset_contexts()
    assert contexts
    gate_name = yaml_fast.safe_load(CI_WORKFLOW.read_text())["jobs"][GATE_JOB]["name"]
    assert gate_name in contexts, f"the gate job's name {gate_name!r} is not required"


_GATED = """
jobs:
  hooks:
    name: hooks (lint + validate + secrets + docs)
    steps: []
  pytest:
    name: pytest
    steps: []
  prek:
    name: prek (lint + validate + tests + secrets)
    needs: [hooks, pytest]
    steps: []
  renovate-config:
    name: renovate config validator
    steps: []
"""

_ADVISORY = (
    _GATED
    + """  smoke:
    name: smoke tests
    steps: []
"""
)


def test_a_fully_gated_workflow_is_clean():
    assert ungated_jobs(_GATED, _ruleset_contexts()) == set()


def test_an_advisory_only_job_is_flagged():
    """The reject half. Without it a function that always returned an empty set would pass."""
    assert ungated_jobs(_ADVISORY, _ruleset_contexts()) == {"smoke"}
