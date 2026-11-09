"""Every job in ci.yml must be gated by a required status context.

`test_ci_contexts_match_workflows.py` checks one direction: every required context names a real
job. Nothing checked the reverse. A fourth job added to ci.yml runs advisory-only unless the
merge gate covers it, and a job whose failure cannot block a merge is a check nobody reads.

The gate is reachable two ways, so both count as gated:

- `prek` re-reports its dependencies under the one required context name.
- A job whose display name is in `gitops_deploy_expected_ruleset_contexts` is required by the
  ruleset directly, as `renovate config validator` is.

A job in `NON_GATING_JOBS` is exempt on purpose, because its verdict must not block a merge.
The exemption holds only while the job carries job-level `continue-on-error: true` and stays out
of the gate's `needs`: `hosts/lib/ci_verdict.py` reads every check-run there, so an exempt job
inside `needs` would still gate.

BEING IN `needs` IS NOT ENOUGH. The gate's step compares each dependency's `needs.<id>.result`
against `success` by name. A `needs` entry the step does not read is WAITED for and never
CHECKED: the step exits 0 on the names it does read, the required context passes, and the merge
goes through with that job red. So a job counts as gated through `prek` only when the step's own
expressions name it.

The context list is read from `hosts/components/github-checks/defaults/main.yml`, the repo's committed
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
KNOWN_JOBS = frozenset(
    {
        "hooks",
        "pr_gates",
        "pytest",
        "ansible_lint",
        "deck_mod",
        "flux_roundtrip",
        "sops_manifest_type_proof",
        "host_packages_build",
        "host_packages",
        "host_tools_arm64",
        "k3s_download",
        GATE_JOB,
        "renovate-config",
    }
)

# Jobs whose failure must not block a merge, each with the reason.
NON_GATING_JOBS = frozenset(
    {
        # install.sh's download of the k3s release; a GitHub outage must not fail a PR (#7754).
        "k3s_download",
    }
)

# The results the gate's step must go on comparing, for the same reason.
# `ansible_lint` rather than `ansible-lint`: a GitHub expression parses a hyphen as
# subtraction, so `needs.ansible-lint.result` is a syntax error. The job id carries the
# underscore and the display name carries the hyphen.
GATE_READS = frozenset(
    {
        "hooks",
        "pr_gates",
        "pytest",
        "ansible_lint",
        "deck_mod",
        "flux_roundtrip",
        "sops_manifest_type_proof",
        "host_packages_build",
        "host_packages",
        "host_tools_arm64",
    }
)

_RESULT_EXPR = re.compile(r"needs\.([A-Za-z0-9_-]+)\.result")


def gate_evaluated_jobs(workflow_text: str) -> set[str]:
    """Job ids the gate's steps actually read, rather than merely wait for.

    Serialising the steps and matching `needs.<id>.result` over the JSON catches the expression
    wherever it sits -- an `env:` value, an `if:`, or inline in `run:`."""
    gate = yaml_fast.safe_load(workflow_text)["jobs"][GATE_JOB]
    return set(_RESULT_EXPR.findall(json.dumps(gate.get("steps") or [])))


def ungated_jobs(
    workflow_text: str,
    required_contexts: set[str],
    exempt: frozenset[str] = frozenset(),
) -> set[str]:
    """Job ids that neither the gate job checks nor a required context names nor `exempt` lists."""
    jobs = yaml_fast.safe_load(workflow_text)["jobs"]
    needs = jobs.get(GATE_JOB, {}).get("needs") or []
    if isinstance(needs, str):
        needs = [needs]
    through_gate = set(needs) & gate_evaluated_jobs(workflow_text)
    return {
        job_id
        for job_id, spec in jobs.items()
        if job_id not in through_gate
        and job_id not in exempt
        and (spec.get("name") or job_id) not in required_contexts
    }


def exemption_problems(workflow_text: str, exempt: frozenset[str]) -> list[str]:
    """Why each exempt job is not a non-gating job: missing, gated anyway, or able to fail the run."""
    jobs = yaml_fast.safe_load(workflow_text)["jobs"]
    needs = jobs.get(GATE_JOB, {}).get("needs") or []
    if isinstance(needs, str):
        needs = [needs]
    problems = []
    for job_id in sorted(exempt):
        if job_id not in jobs:
            problems.append(f"{job_id}: no such job")
            continue
        if job_id in needs:
            problems.append(f"{job_id}: in {GATE_JOB}'s needs, so it gates")
        if jobs[job_id].get("continue-on-error") is not True:
            problems.append(f"{job_id}: no job-level `continue-on-error: true`")
    return problems


def _ruleset_contexts() -> set[str]:
    """The merge gate, read from the YAML defaults as a literal list -- the same regex shape the
    sibling guard uses for the deployer's role defaults."""
    text = (
        REPO / "hosts" / "components" / "github-checks" / "defaults" / "main.yml"
    ).read_text()
    block = re.search(
        r"^gitops_deploy_expected_ruleset_contexts:\s*\n((?:\s*-\s.+\n)+)", text, re.M
    )
    assert block, "gitops_deploy_expected_ruleset_contexts is not a literal list"
    return {line.strip().lstrip("-").strip() for line in block.group(1).splitlines()}


def test_every_ci_job_is_gated_by_a_required_context():
    ungated = ungated_jobs(
        CI_WORKFLOW.read_text(), _ruleset_contexts(), NON_GATING_JOBS
    )
    assert not ungated, (
        f"ci.yml jobs that block no merge: {sorted(ungated)}. Add each to the `prek` job's "
        "`needs` AND to the result comparison in its step, or make it a required context in "
        "the ruleset and record it in gitops_deploy_expected_ruleset_contexts."
    )


def test_every_non_gating_job_is_outside_the_gate_and_continues_on_error():
    assert exemption_problems(CI_WORKFLOW.read_text(), NON_GATING_JOBS) == []


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

# A job in neither the gate's `needs` nor the ruleset: advisory-only.
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


_NON_GATING = (
    _GATED
    + """  smoke:
    name: smoke tests
    continue-on-error: true
    steps: []
"""
)


def test_an_exempt_job_outside_the_gate_is_clean():
    assert ungated_jobs(_NON_GATING, _ruleset_contexts(), frozenset({"smoke"})) == set()
    assert exemption_problems(_NON_GATING, frozenset({"smoke"})) == []


def test_an_exempt_job_that_can_fail_the_run_is_flagged():
    assert exemption_problems(_ADVISORY, frozenset({"smoke"})) == [
        "smoke: no job-level `continue-on-error: true`"
    ]


def test_an_exempt_job_in_the_gates_needs_is_flagged():
    gated = _NON_GATING.replace(
        "needs: [hooks, pytest]", "needs: [hooks, pytest, smoke]"
    )
    assert exemption_problems(gated, frozenset({"smoke"})) == [
        "smoke: in prek's needs, so it gates"
    ]


def test_an_exemption_naming_no_job_is_flagged():
    assert exemption_problems(_GATED, frozenset({"smoke"})) == ["smoke: no such job"]
