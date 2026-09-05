"""Every job in ci.yml must be gated by a required status context.

`test_ci_contexts_match_workflows.py` checks one direction: every required context names a real
job. Nothing checked the reverse. A fourth job added to ci.yml runs advisory-only unless the
merge gate covers it, and a job whose failure cannot block a merge is a check nobody reads.

The gate is reachable two ways, so both count as gated:

- `prek` re-reports its dependencies under the one required context name.
- A job whose display name is in `gitops_deploy_expected_ruleset_contexts` is required by the
  ruleset directly, as `renovate config validator` is.

BEING IN `needs` IS NOT ENOUGH. The gate's step compares `needs.hooks.result` and
`needs.pytest.result` against `success` by name. A third `needs` entry the step does not read is
WAITED for and never CHECKED: the step exits 0 on the two names it does read, the required
context passes, and the merge goes through with that job red. So a job counts as gated through
`prek` only when the step's own expressions name it.

The context list is read from `roles/setup/gitops_deploy/defaults/main.yml`, the repo's committed
copy of ruleset 20912512. `github-ruleset-drift.sh` compares that copy against the live ruleset
daily, so this guard needs no network and still tracks the real gate.

Run: uv run pytest ansible/tests/repo/test_every_ci_job_is_gated.py
"""

import json
import re

from _helpers import REPO
from lib import yaml_fast

CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"

# The job that re-reports the required context for the halves it depends on.
GATE_JOB = "prek"

# Jobs ci.yml is known to declare. A parser that returned an empty mapping would make the subset
# check below vacuously true, so a rename fails here rather than silently shrinking coverage.
KNOWN_JOBS = frozenset({"hooks", "pytest", GATE_JOB, "renovate-config"})

# The results the gate's step must go on comparing, for the same reason.
GATE_READS = frozenset({"hooks", "pytest"})

_RESULT_EXPR = re.compile(r"needs\.([A-Za-z0-9_-]+)\.result")


def gate_evaluated_jobs(workflow_text: str) -> set[str]:
    """Job ids the gate's steps actually read, rather than merely wait for.

    Serialising the steps and matching `needs.<id>.result` over the JSON catches the expression
    wherever it sits -- an `env:` value, an `if:`, or inline in `run:`."""
    gate = yaml_fast.safe_load(workflow_text)["jobs"][GATE_JOB]
    return set(_RESULT_EXPR.findall(json.dumps(gate.get("steps") or [])))


def ungated_jobs(workflow_text: str, required_contexts: set[str]) -> set[str]:
    """Job ids that neither the gate job checks nor a required context names."""
    jobs = yaml_fast.safe_load(workflow_text)["jobs"]
    needs = jobs.get(GATE_JOB, {}).get("needs") or []
    if isinstance(needs, str):
        needs = [needs]
    through_gate = set(needs) & gate_evaluated_jobs(workflow_text)
    return {
        job_id
        for job_id, spec in jobs.items()
        if job_id not in through_gate
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
        "`needs` AND to the result comparison in its step, or make it a required context in "
        "the ruleset and record it in gitops_deploy_expected_ruleset_contexts."
    )


def test_the_workflow_parse_finds_the_jobs_it_must_find():
    """Non-vacuity. An empty jobs mapping passes the check above while covering nothing."""
    jobs = set(yaml_fast.safe_load(CI_WORKFLOW.read_text())["jobs"])
    assert KNOWN_JOBS <= jobs, f"ci.yml no longer declares {sorted(KNOWN_JOBS - jobs)}"


def test_the_gate_step_parse_finds_the_results_it_reads():
    """Non-vacuity. An empty evaluated set would flag both halves rather than nothing, but it
    would also stop this guard from distinguishing a checked `needs` entry from an ignored one.

    `>=`, not `==`: the guard above tells an author to add a new job to BOTH the gate's `needs`
    and its result comparison, and an equality here would fail on them doing exactly that. A
    misspelt expression is still caught -- the real job drops out of `through_gate` and
    `test_every_ci_job_is_gated_by_a_required_context` flags it."""
    assert GATE_READS <= gate_evaluated_jobs(CI_WORKFLOW.read_text())


def test_the_context_parse_finds_the_gate_jobs_name():
    contexts = _ruleset_contexts()
    assert contexts
    gate_name = yaml_fast.safe_load(CI_WORKFLOW.read_text())["jobs"][GATE_JOB]["name"]
    assert gate_name in contexts, f"the gate job's name {gate_name!r} is not required"


_GATE_STEP = """    steps:
      - env:
          HOOKS: ${{ needs.hooks.result }}
          PYTEST: ${{ needs.pytest.result }}
        run: '[ "$HOOKS" = success ] && [ "$PYTEST" = success ]'
"""

_GATED = (
    """
jobs:
  hooks:
    name: hooks (lint + validate + secrets + docs)
    steps: []
  pytest:
    name: pytest
    steps: []
  renovate-config:
    name: renovate config validator
    steps: []
  prek:
    name: prek (lint + validate + tests + secrets)
    needs: [hooks, pytest]
    if: always()
"""
    + _GATE_STEP
)

# A job in neither the gate's `needs` nor the ruleset: advisory-only, the shape #1127 names.
_ADVISORY = (
    _GATED
    + """  smoke:
    name: smoke tests
    steps: []
"""
)

# A job the gate WAITS for and never CHECKS: advisory-only too, and the one that reads as gated.
_UNCHECKED_NEED = _ADVISORY.replace(
    "needs: [hooks, pytest]", "needs: [hooks, pytest, smoke]"
)


def test_a_fully_gated_workflow_is_clean():
    assert ungated_jobs(_GATED, _ruleset_contexts()) == set()


def test_an_advisory_only_job_is_flagged():
    """The reject half. Without it a function that always returned an empty set would pass."""
    assert ungated_jobs(_ADVISORY, _ruleset_contexts()) == {"smoke"}


def test_a_needs_entry_the_gate_never_reads_is_flagged():
    """The second reject half, and the reason `needs` membership alone is not the test: this
    workflow waits for `smoke` and merges green when it fails."""
    assert ungated_jobs(_UNCHECKED_NEED, _ruleset_contexts()) == {"smoke"}
