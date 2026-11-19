"""A PR that changes only the container cases' inputs runs those cases, on both legs.

`host_packages` in ci.yml runs its container cases (H3.3b) when the render diff in
`host_packages_build` reports `changed=true`, when `host_packages.py cases-changed` reports that
the PR changes an input of the cases, or on a dispatch. That command's decision is unit-tested in
`scripts/validate/tests/test_validate_host_packages.py`. This module holds the workflow's wiring
of it: the render step writes the verdict to `cases`, and every gate between it and the cases
reads that output.

Run: uv run pytest tests/repo/test_host_package_cases_run_on_their_inputs.py
"""

import re

from _helpers import REPO
from lib import yaml_fast

CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
BUILD_JOB = "host_packages_build"
JOB = "host_packages"

CASES_STEP = "Run the updater cases in systemd containers"
# The cases glob each side's updater .deb out of the build step's output and boot the image the
# image step tags, so a run that skips either fails the cases rather than running them.
BUILD_STEP = "Build both renders with the pinned nfpm"
IMAGE_STEP = "Build the test image for this release"
CASES_READ = "steps.render.outputs.cases == 'true'"


def _jobs() -> dict:
    return yaml_fast.safe_load(CI_WORKFLOW.read_text())["jobs"]


def _step(job: dict, name: str) -> dict:
    (step,) = [s for s in job["steps"] if s.get("name") == name]
    return step


def test_the_cases_gates_read_the_cases_changed_verdict() -> None:
    jobs = _jobs()
    build, cases_job = jobs[BUILD_JOB], jobs[JOB]
    (render,) = [s for s in build["steps"] if s.get("id") == "render"]
    assert re.search(
        r'cases=\$\(uv run python scripts/validate/host_packages\.py cases-changed "\$base" \\\n'
        r'\s+"\$RUNNER_TEMP/base-tree" --inputs "\$CASE_INPUTS"\)\n'
        r'echo "cases=\$cases" >> "\$GITHUB_OUTPUT"',
        render["run"],
    ), "the render step no longer writes cases-changed's verdict to `cases`"
    assert CASES_READ in build["outputs"]["run-cases"]
    assert CASES_READ in _step(build, BUILD_STEP)["if"]
    assert "needs.host_packages_build.outputs.run-cases == 'true'" in cases_job["if"]
    for name in (IMAGE_STEP, CASES_STEP):
        assert "if" not in _step(cases_job, name), (
            f"{name!r} has its own `if:`, so the job's `run-cases` gate no longer decides it"
        )


def _artifacts(job: dict, action: str) -> set[str]:
    """The artifact names a job's `actions/<action>-artifact` steps name."""
    return {
        step["with"]["name"]
        for step in job["steps"]
        if str(step.get("uses", "")).startswith(f"actions/{action}-artifact@")
    }


def test_the_arm64_job_and_the_cases_legs_wait_for_the_build_alone() -> None:
    """Neither consumer waits for the other, and each reads what the build uploads."""
    jobs = _jobs()
    built = _artifacts(jobs[BUILD_JOB], "upload")
    for consumer in (JOB, "host_tools_arm64"):
        assert jobs[consumer]["needs"] == ["prove", BUILD_JOB], consumer
        downloads = _artifacts(jobs[consumer], "download")
        assert downloads, f"{consumer} downloads nothing from {BUILD_JOB}"
        assert downloads <= built, (
            f"{consumer} downloads {downloads - built}, never uploaded"
        )


def test_the_cases_legs_restore_the_cache_the_build_fetched() -> None:
    """A pin the build fetched is not requested again by a cases leg.

    The legs restore it from actions/cache under the build's own key, not from the tar.
    """
    jobs = _jobs()
    build = "\n".join(str(step.get("run", "")) for step in jobs[BUILD_JOB]["steps"])
    assert '--cache "$RUNNER_TEMP/tools-cache"' in build
    (tar,) = re.findall(r'host-packages-build\.tar" -C "\$RUNNER_TEMP" (.*)', build)
    assert "tools-cache" not in tar
    assert (
        jobs[BUILD_JOB]["outputs"]["tools-key"] == "${{ steps.tools-key.outputs.key }}"
    )
    (restore,) = [s for s in jobs[JOB]["steps"] if s.get("id") == "tools-cache"]
    assert restore["uses"].startswith("actions/cache/restore@")
    assert restore["with"] == {
        "path": "${{ runner.temp }}/tools-cache",
        "key": f"${{{{ needs.{BUILD_JOB}.outputs.tools-key }}}}",
    }
    cases = _step(jobs[JOB], CASES_STEP)
    assert (
        cases["env"]["TOOLS_CACHE_HIT"] == "${{ steps.tools-cache.outputs.cache-hit }}"
    )
    assert 'seed=(--tools-cache "$RUNNER_TEMP/tools-cache")' in cases["run"]
