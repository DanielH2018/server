"""The CI verdict decides from the gate's `needs` jobs while the gate job has not concluded (#5507).

Master run 39074978426 finished every real job at 22:39:45Z, and its ~3 second `prek` gate job
then sat queued for a runner past 22:51, so two landings gave up with `ci-timeout`. `ci_verdict`
now applies the gate's own rule to the jobs' check-runs, and believes a gate that has concluded.
Every case that must pass has a sibling that must not.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_deploy_ci_jobs_verdict.py
"""

# ansible/roles/setup/gitops_deploy/tests/test_deploy_ci_jobs_verdict.py

from deployer_lib.deploy_git import (
    CI_GATE_NAME,
    CI_GATE_NEEDS_JOBS,
    CI_PROVE_JOB,
    ci_verdict,
    jobs_verdict,
)

_REQUIRED = frozenset({CI_GATE_NAME})
_JOB_NAMES = sorted(CI_GATE_NEEDS_JOBS)
_QUEUED_GATE = {"name": CI_GATE_NAME, "status": "queued", "conclusion": None}


def _run(name, status="completed", conclusion="success"):
    return {"name": name, "status": status, "conclusion": conclusion}


def _jobs(conclusion="success", **per_job):
    """One completed check-run per gate dependency; `per_job` maps a name to its conclusion."""
    return [_run(n, conclusion=per_job.get(n, conclusion)) for n in _JOB_NAMES]


def _push_run(proven=False):
    """A push run: `prove` succeeded, and every dependent skipped when the tree was proven."""
    return [_run(CI_PROVE_JOB)] + _jobs("skipped" if proven else "success")


def test_green_while_the_gate_job_is_still_queued():
    assert ci_verdict(_push_run() + [_QUEUED_GATE], _REQUIRED) == "pass"


def test_green_when_the_gate_job_has_not_been_created_yet():
    assert ci_verdict(_push_run(), _REQUIRED) == "pass"


def test_green_on_the_proven_path_while_the_gate_job_is_still_queued():
    assert ci_verdict(_push_run(proven=True) + [_QUEUED_GATE], _REQUIRED) == "pass"


def test_green_on_a_run_that_is_not_a_push():
    runs = [_run(CI_PROVE_JOB, conclusion="skipped")] + _jobs() + [_QUEUED_GATE]
    assert ci_verdict(runs, _REQUIRED) == "pass"


def test_red_when_a_required_job_failed_and_the_gate_is_queued():
    runs = [_run(CI_PROVE_JOB)] + _jobs(**{_JOB_NAMES[0]: "failure"}) + [_QUEUED_GATE]
    assert ci_verdict(runs, _REQUIRED) == "fail"


def test_pending_when_a_required_job_has_not_reported():
    runs = [_run(CI_PROVE_JOB)] + _jobs()[1:] + [_QUEUED_GATE]
    assert ci_verdict(runs, _REQUIRED) == "pending"


def test_pending_when_prove_has_not_reported():
    assert ci_verdict(_jobs() + [_QUEUED_GATE], _REQUIRED) == "pending"


def test_pending_while_a_required_job_is_still_running():
    runs = [_run(CI_PROVE_JOB)] + _jobs()
    runs[1] = _run(_JOB_NAMES[0], status="in_progress", conclusion=None)
    assert ci_verdict(runs + [_QUEUED_GATE], _REQUIRED) == "pending"


def test_a_skipped_job_without_proof_is_not_a_pass():
    one_skipped = [_run(CI_PROVE_JOB)] + _jobs(**{_JOB_NAMES[0]: "skipped"})
    assert jobs_verdict(one_skipped) == "pending"
    not_a_push = [_run(CI_PROVE_JOB, conclusion="skipped")] + _jobs("skipped")
    assert jobs_verdict(not_a_push) == "pending"
    assert jobs_verdict(_jobs("skipped")) == "pending"


def test_a_failed_prove_job_lets_the_full_sweep_decide():
    """`tested_tree.py` exits 0 on every answer, so a failed `prove` is a runner fault (#5783)."""
    failed = _run(CI_PROVE_JOB, conclusion="failure")
    assert jobs_verdict([failed] + _jobs()) == "pass"
    assert jobs_verdict([failed] + _jobs(**{_JOB_NAMES[0]: "failure"})) == "fail"
    assert jobs_verdict([failed] + _jobs("skipped")) == "pending"


def test_a_timed_out_prove_job_lets_the_full_sweep_decide():
    """A cancelled `prove` proved nothing, so every dependent runs and decides (#5783)."""
    timed_out = _run(CI_PROVE_JOB, conclusion="cancelled")
    assert ci_verdict([timed_out] + _jobs() + [_QUEUED_GATE], _REQUIRED) == "pass"
    red = [timed_out] + _jobs(**{_JOB_NAMES[0]: "failure"})
    assert jobs_verdict(red) == "fail"
    # A skip is proof only beside a `prove` that succeeded.
    assert jobs_verdict([timed_out] + _jobs("skipped")) == "pending"


def test_a_concluded_gate_is_believed_over_the_jobs():
    red_gate = _push_run() + [_run(CI_GATE_NAME, conclusion="failure")]
    assert ci_verdict(red_gate, _REQUIRED) == "fail"
    assert ci_verdict([_run(CI_GATE_NAME)], _REQUIRED) == "pass"


def test_the_job_rule_applies_only_when_the_gate_is_required():
    other = "renovate config validator"
    runs = _push_run() + [_run(other, status="queued", conclusion=None)]
    assert ci_verdict(runs, frozenset({other})) == "pending"


def _draft_run():
    """A draft PR's run (#5563): every job skips, the gate included."""
    return (
        [_run(CI_PROVE_JOB, conclusion="skipped")]
        + _jobs("skipped")
        + [_run(CI_GATE_NAME, conclusion="skipped")]
    )


def test_a_head_whose_only_gate_is_a_draft_runs_skipped_gate_is_pending():
    """`land.sh` must not merge a PR marked ready before its ready run registers."""
    assert ci_verdict(_draft_run(), _REQUIRED) == "pending"


def test_the_ready_run_decides_a_head_the_draft_run_also_reported_on():
    ready = [_run(CI_PROVE_JOB, conclusion="skipped")] + _jobs()
    assert ci_verdict(_draft_run() + ready + [_run(CI_GATE_NAME)], _REQUIRED) == "pass"
    assert ci_verdict(_draft_run() + ready + [_QUEUED_GATE], _REQUIRED) == "pass"
    red = ready + [_run(CI_GATE_NAME, conclusion="failure")]
    assert ci_verdict(_draft_run() + red, _REQUIRED) == "fail"
