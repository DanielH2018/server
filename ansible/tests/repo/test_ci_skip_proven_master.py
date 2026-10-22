"""A master push whose tree a green PR run already tested skips the repeated jobs (#5489).

WHY THIS IS A TEST. Three properties hold the design together, and each fails silently:

- A PR run is a full sweep. If a hook or ansible-lint were scoped to changed files again, the
  push run that the proof skips would be the only place the scoped-out files were ever checked.
- Every job that repeats on a push waits for `prove` and skips only on `proven == 'true'`. A job
  that dropped the condition would run on the proven path and cost what the change saves, and a
  job that dropped `needs: prove` would read an unset output.
- The `prek` gate, the check-run that the deployer's `ci_verdict` and `await_ci.py` read, passes
  on the proven path and fails on a skipped job anywhere else. GitHub counts a skipped required
  check as passing, so a gate that accepted `skipped` unconditionally would merge a red-by-skip.

The gate's pass condition is run, not read: its `run:` script is extracted from ci.yml and
executed against each combination of results.

Run: uv run pytest ansible/tests/repo/test_ci_skip_proven_master.py
"""

import subprocess

import pytest
from _helpers import REPO
from lib import yaml_fast

CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
GATE_NAME = "prek (lint + validate + tests + secrets)"
# The draft arm skips every job on a draft PR (#5563); `test_ci_skips_draft_prs` below says why.
DRAFT_ARM = "github.event.pull_request.draft != true"
PROOF_CONDITION = (
    "(!cancelled() || needs.prove.result == 'cancelled')"
    f" && needs.prove.outputs.proven != 'true' && {DRAFT_ARM}"
)
DEPENDENTS = (
    "hooks",
    "pytest",
    "ansible_lint",
    "deck_mod",
    "flux_roundtrip",
    "sops_manifest_type_proof",
    "host_packages",
)
# The env names the gate's script reads, one per dependency, in the order of DEPENDENTS.
GATE_ENV = (
    "HOOKS",
    "PYTEST",
    "ANSIBLE_LINT",
    "DECK_MOD",
    "FLUX_ROUNDTRIP",
    "SOPS_TYPE_PROOF",
    "HOST_PACKAGES",
)


def _jobs() -> dict:
    return yaml_fast.safe_load(CI_WORKFLOW.read_text())["jobs"]


def _normal(condition: object) -> str:
    return str(condition).strip().removeprefix("${{").removesuffix("}}").strip()


def test_pr_runs_do_not_scope_the_hooks_or_ansible_lint():
    """The hooks and ansible-lint jobs have one sweep step each, and it is unconditional."""
    jobs = _jobs()
    for job, step_name in (
        ("hooks", "Run hooks (full sweep)"),
        ("ansible_lint", "Lint the whole tree"),
    ):
        steps = {s["name"]: s for s in jobs[job]["steps"] if "name" in s}
        assert "if" not in steps[step_name], (
            f"{job}: {step_name!r} is conditional again"
        )
    text = CI_WORKFLOW.read_text()
    for gone in ("FULL_SWEEP_PATHS", "mode=scoped", '--files "${changed[@]}"'):
        assert gone not in text, f"ci.yml scopes a PR run again: {gone!r}"
    sweeps = [
        s["run"]
        for s in jobs["hooks"]["steps"] + jobs["ansible_lint"]["steps"]
        if "prek run" in str(s.get("run", ""))
    ]
    assert sweeps and all("--all-files" in run for run in sweeps), sweeps


def test_the_prove_job_runs_on_a_push_only_and_reads_the_tested_tree_script():
    prove = _jobs()["prove"]
    assert _normal(prove["if"]) == "github.event_name == 'push'"
    script = "\n".join(str(s.get("run", "")) for s in prove["steps"])
    assert "scripts/dev/tested_tree.py prove" in script
    assert prove["outputs"]["proven"] == "${{ steps.prove.outputs.proven }}"
    assert set(prove["permissions"]) == {"contents", "pull-requests", "statuses"}
    assert prove["permissions"]["statuses"] == "read"


def test_every_repeated_job_waits_for_the_proof_and_skips_on_it():
    jobs = _jobs()
    for job in DEPENDENTS:
        assert jobs[job].get("needs") == "prove", f"{job} does not wait for `prove`"
        assert _normal(jobs[job].get("if")) == PROOF_CONDITION, (
            f"{job} does not skip on the proof, or does not survive a skipped `prove`"
        )
    # renovate-config skips on a push already, and the ruleset requires it on a PR.
    assert (
        _normal(jobs["renovate-config"]["if"])
        == f"github.event_name != 'push' && {DRAFT_ARM}"
    )


def test_ci_skips_draft_prs_and_runs_when_one_is_marked_ready():
    """A draft PR runs no job, and `ready_for_review` starts its first real run (#5563).

    A fan-out batch opens its PR as a draft and pushes while it is reviewed. A job without the
    draft arm would run on every such push. Without `ready_for_review` in `types`, marking the
    PR ready would start nothing, and the PR would reach the landing with no CI at all.
    """
    pull_request = yaml_fast.safe_load(CI_WORKFLOW.read_text())[True]["pull_request"]
    assert pull_request["types"] == [
        "opened",
        "synchronize",
        "reopened",
        "ready_for_review",
    ]
    jobs = _jobs()
    assert {"prek", "renovate-config", *DEPENDENTS} <= set(jobs)
    # `prove` runs on a push only, which never carries a pull request.
    unguarded = [
        name
        for name, job in jobs.items()
        if name != "prove" and DRAFT_ARM not in _normal(job.get("if"))
    ]
    assert unguarded == []


def test_the_statuses_write_permission_is_on_the_gate_alone():
    jobs = _jobs()
    writers = [
        n for n, j in jobs.items() if "write" in (j.get("permissions") or {}).values()
    ]
    assert writers == ["prek"]
    assert jobs["prek"]["permissions"] == {"contents": "read", "statuses": "write"}
    assert yaml_fast.safe_load(CI_WORKFLOW.read_text())["permissions"] == {
        "contents": "read"
    }


def test_only_a_same_repo_pr_run_records_the_tested_tree():
    steps = {s["name"]: s for s in _jobs()["prek"]["steps"] if "name" in s}
    record = steps["Record the tested tree on the PR head"]
    assert "github.event_name == 'pull_request'" in record["if"]
    assert "head.repo.full_name == github.repository" in record["if"]
    assert record["continue-on-error"] is True
    assert "ci/tested-tree" in record["run"]
    assert "ci/tested-tree" in (REPO / "scripts/dev/tested_tree.py").read_text()


def _gate_exit(prove: str, proven: str, results: dict[str, str]) -> int:
    """Run the gate's own script with these results; its exit status is the check-run's."""
    gate = _jobs()["prek"]
    check = next(s for s in gate["steps"] if "env" in s and "HOOKS" in s["env"])
    env = {"PROVE": prove, "PROVEN": proven, "PATH": "/usr/bin:/bin"}
    env.update(
        {name: results[job] for name, job in zip(GATE_ENV, DEPENDENTS, strict=True)}
    )
    return subprocess.run(["bash", "-c", check["run"]], env=env, timeout=30).returncode


def _all(result: str, **override: str) -> dict[str, str]:
    return {job: override.get(job, result) for job in DEPENDENTS}


def test_the_gate_keeps_its_required_name_and_always_runs():
    gate = _jobs()["prek"]
    assert gate["name"] == GATE_NAME
    assert _normal(gate["if"]) == f"always() && {DRAFT_ARM}"
    assert "prove" in gate["needs"]


@pytest.mark.parametrize(
    ("prove", "proven", "results", "passes"),
    [
        # An ordinary PR run, and a push the proof declined: everything must have succeeded.
        ("skipped", "", _all("success"), True),
        ("success", "false", _all("success"), True),
        ("success", "", _all("success"), True),
        ("skipped", "", _all("success", hooks="failure"), False),
        # The proven path: every dependent skipped, and the gate is green.
        ("success", "true", _all("skipped"), True),
        ("success", "true", _all("skipped", pytest="failure"), False),
        # A skip without a proof is a failure.
        ("success", "false", _all("skipped"), False),
        ("skipped", "", _all("skipped"), False),
        ("success", "", _all("success", host_packages="skipped"), False),
        # `proven` alone is not a proof when the job that wrote it did not succeed.
        ("failure", "true", _all("skipped"), False),
        ("cancelled", "", _all("cancelled"), False),
        # A timed-out `prove` proved nothing, so the full sweep decides (#5783).
        ("cancelled", "", _all("success"), True),
        ("cancelled", "", _all("skipped"), False),
        ("cancelled", "", _all("success", pytest="failure"), False),
    ],
)
def test_the_gate_passes_on_the_proven_path_and_nowhere_a_skip_is_unproven(
    prove, proven, results, passes
):
    assert (_gate_exit(prove, proven, results) == 0) is passes
