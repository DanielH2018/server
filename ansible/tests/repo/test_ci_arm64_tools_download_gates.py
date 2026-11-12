"""`host_tools_arm64` fails when the arm64 homelab-tools build exists and its download does not.

The download step carried `continue-on-error`, because a run that builds nothing has no artifact
to fetch. A failed download looked the same, so the job went green having tested nothing. On
2026-11-11 run 43094935083 logged `Unable to download artifact(s): Artifact not found for name:
homelab-tools-arm64`. `host_packages_build` now says whether it built the .deb, and the arm64
job downloads only then, with no `continue-on-error`.

Run: uv run pytest ansible/tests/repo/test_ci_arm64_tools_download_gates.py
"""

import copy
import os
from pathlib import Path

import pytest
from _helpers import REPO
from lib import yaml_fast
from lib.proc_testing import run

CI = REPO / ".github" / "workflows" / "ci.yml"
BUILD, ARM64 = "host_packages_build", "host_tools_arm64"
OUTPUT = "${{ steps.arm64.outputs.built }}"
ARTIFACT = "homelab-tools-arm64"
INSTALL = "Install the build in the test image and run each tool"


def _download(wf: dict) -> dict:
    steps = (wf.get("jobs") or {}).get(ARM64, {}).get("steps") or []
    return next((s for s in steps if (s.get("with") or {}).get("name") == ARTIFACT), {})


def gate_problems(wf: dict) -> list[str]:
    """Every way the arm64 job could read a failed download as nothing built."""
    build = (wf.get("jobs") or {}).get(BUILD, {})
    download = _download(wf)
    found = []
    if (build.get("outputs") or {}).get("arm64-tools") != OUTPUT:
        found.append(f"{BUILD}: no output arm64-tools set from {OUTPUT}")
    if download.get("continue-on-error"):
        found.append(f"{ARM64}: the download carries continue-on-error")
    if "outputs.arm64-tools" not in str(download.get("if", "")):
        found.append(f"{ARM64}: the download does not read the build's arm64-tools")
    return found


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml_fast.safe_load(CI.read_text(encoding="utf-8"))


def test_a_failed_download_fails_the_arm64_job_is_clean(workflow):
    assert gate_problems(workflow) == []


def _soften_download(wf):
    _download(wf)["continue-on-error"] = True


def _drop_output(wf):
    del wf["jobs"][BUILD]["outputs"]["arm64-tools"]


RED = {
    "a download that continues on error": (_soften_download, "continue-on-error"),
    "a build job with no output": (_drop_output, "no output arm64-tools"),
}


@pytest.mark.parametrize("case", RED, ids=list(RED))
def test_a_download_failure_that_could_pass_is_flagged(workflow, case):
    mutate, expected = RED[case]
    broken = copy.deepcopy(workflow)
    mutate(broken)
    assert any(expected in line for line in gate_problems(broken)), gate_problems(
        broken
    )


@pytest.mark.parametrize(
    ("debs", "built"),
    [
        (["homelab-tools_1.0_arm64.deb"], "true"),
        (["homelab-tools_1.0_amd64.deb"], "false"),
    ],
    ids=["an-arm64-build", "an-amd64-build-only"],
)
def test_the_step_reports_whether_an_arm64_deb_exists(
    workflow, tmp_path: Path, debs: list[str], built: str
):
    head = tmp_path / "debs" / "head"
    head.mkdir(parents=True)
    for name in debs:
        (head / name).touch()
    output = tmp_path / "output"
    script = next(
        s for s in workflow["jobs"][BUILD]["steps"] if s.get("id") == "arm64"
    )["run"]
    env = {**os.environ, "RUNNER_TEMP": str(tmp_path), "GITHUB_OUTPUT": str(output)}
    result = run(["bash", "-c", script], env=env)
    assert result.returncode == 0, result.stderr
    assert output.read_text() == f"built={built}\n"


@pytest.mark.parametrize(
    ("built", "rc"),
    [("true", 1), ("false", 0)],
    ids=["a-build-the-download-lost", "no-build"],
)
def test_the_install_fails_only_when_a_built_deb_is_missing(
    workflow, tmp_path: Path, built: str, rc: int
):
    script = next(
        s for s in workflow["jobs"][ARM64]["steps"] if s.get("name") == INSTALL
    )["run"]
    env = {**os.environ, "RUNNER_TEMP": str(tmp_path), "BUILT": built}
    result = run(["bash", "-c", script], env=env)
    assert result.returncode == rc, result.stdout + result.stderr
