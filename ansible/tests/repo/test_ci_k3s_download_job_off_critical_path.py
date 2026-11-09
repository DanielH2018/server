"""The non-gating k3s download case runs in its own CI job, off the host_packages legs (#7810).

As a step of the `host_packages (26.04, ...)` leg it ran serially after the gating cases and
added 80-109 s to the longest leg of the PR. A job of its own that needs only `prove` and
`host_packages_build` runs beside the legs instead.

Run: uv run pytest ansible/tests/repo/test_ci_k3s_download_job_off_critical_path.py
"""

from _helpers import REPO
from lib import yaml_fast

CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
CASES_ENTRY = "scripts/validate/host_package_cases.py"
DOWNLOAD_FLAG = "--k3s-download"
GATING_JOB = "host_packages"
GATE_JOB = "prek"
HANDOFF_ARTIFACT = "host-packages-build"
IMAGE_SCRIPT = "hosts/lib/tests/container/build-image.sh"


def _jobs() -> dict:
    return yaml_fast.safe_load(CI_WORKFLOW.read_text())["jobs"]


def _runs_download_case(step: dict) -> bool:
    run = str(step.get("run", ""))
    return CASES_ENTRY in run and DOWNLOAD_FLAG in run


def _download_job() -> tuple[str, dict]:
    """The one ci.yml job that runs the k3s download case, which must not be a host_packages leg."""
    jobs = _jobs()
    found = [
        job_id
        for job_id, job in jobs.items()
        if any(_runs_download_case(step) for step in job.get("steps") or ())
    ]
    assert len(found) == 1, f"jobs running {CASES_ENTRY} {DOWNLOAD_FLAG}: {found}"
    assert found[0] != GATING_JOB, (
        f"the k3s download case still runs inside {GATING_JOB}, on its critical path"
    )
    return found[0], jobs[found[0]]


def test_no_host_packages_step_runs_the_k3s_download_case():
    steps = _jobs()[GATING_JOB].get("steps") or ()
    assert any(CASES_ENTRY in str(s.get("run", "")) for s in steps), (
        f"{GATING_JOB} no longer runs {CASES_ENTRY} at all, so this check is vacuous"
    )
    offending = [s.get("name", "") for s in steps if _runs_download_case(s)]
    assert not offending, f"{GATING_JOB} steps still run {DOWNLOAD_FLAG}: {offending}"


def test_the_k3s_download_job_needs_only_prove_and_the_build():
    job_id, job = _download_job()
    needs = job.get("needs") or []
    if isinstance(needs, str):
        needs = [needs]
    assert set(needs) == {"prove", "host_packages_build"}, (
        f"{job_id} needs {needs}; waiting on {GATING_JOB} puts it back on the critical path"
    )


def test_the_k3s_download_job_continues_on_error():
    job_id, job = _download_job()
    assert job.get("continue-on-error") is True, (
        f"{job_id} lacks a job-level `continue-on-error: true`"
    )


def test_the_k3s_download_job_is_outside_the_gate():
    job_id, _ = _download_job()
    gate_needs = _jobs()[GATE_JOB].get("needs") or []
    assert job_id not in gate_needs, (
        f"{job_id} is in {GATE_JOB}'s needs, and ci_verdict reads every check-run there,"
        " so a k3s release outage would fail the PR"
    )


def test_the_k3s_download_job_downloads_the_build_hand_off():
    job_id, job = _download_job()
    downloads = [
        s
        for s in job.get("steps") or ()
        if str(s.get("uses", "")).startswith("actions/download-artifact@")
        and (s.get("with") or {}).get("name") == HANDOFF_ARTIFACT
    ]
    assert downloads, f"{job_id} does not download the {HANDOFF_ARTIFACT} artifact"


def test_the_k3s_download_job_gets_its_image_like_the_legs():
    job_id, job = _download_job()
    runs = [str(s.get("run", "")) for s in job.get("steps") or ()]
    assert any(IMAGE_SCRIPT in run for run in runs), (
        f"{job_id} never runs {IMAGE_SCRIPT}, so it has no pinned test image to boot"
    )


def test_the_k3s_download_job_skips_or_budgets_for_a_built_image():
    """A Dockerfile PR builds the image (no RepoDigests); the job skips the case or is sized for the build."""
    job_id, job = _download_job()
    case_steps = [s for s in job.get("steps") or () if _runs_download_case(s)]
    checks_digests = any("RepoDigests" in str(s.get("run", "")) for s in case_steps)
    budget = job.get("timeout-minutes")
    sized_for_build = (
        budget is not None and int(budget) >= _jobs()[GATING_JOB]["timeout-minutes"]
    )
    assert checks_digests or sized_for_build, (
        f"{job_id} runs the case on a built image without a timeout sized for the build"
    )


def test_the_k3s_download_jobs_case_and_image_steps_continue_on_error():
    """A job-level `continue-on-error` leaves a failed job's check-run red in the PR's rollup."""
    job_id, job = _download_job()
    steps = job.get("steps") or ()
    image_or_case = [
        s
        for s in steps
        if _runs_download_case(s) or IMAGE_SCRIPT in str(s.get("run", ""))
    ]
    assert len(image_or_case) == 2, f"{job_id}: {[s.get('name') for s in steps]}"
    red = [
        s.get("name") for s in image_or_case if s.get("continue-on-error") is not True
    ]
    assert not red, f"{job_id} steps that turn its check-run red: {red}"
