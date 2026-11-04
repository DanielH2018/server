"""The host_packages test image's layer cache: what build-image.sh asks for, and who seeds it.

`build-image.sh` runs against a stub `docker`, so each test reads the argv a real run would pass.
The census half holds the seeder's jobs equal to ci.yml's build sites: a site with no seeder
builds from the mirror on every run and nothing goes red.

Run: uv run pytest ansible/tests/repo/test_host_test_image_cache.py
"""

import re
import subprocess
from pathlib import Path

import pytest
from _helpers import REPO
from lib import yaml_fast
from lib.proc_testing import fake_bin, run

SCRIPT = REPO / "hosts" / "lib" / "tests" / "container" / "build-image.sh"
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
SEEDER = REPO / ".github" / "workflows" / "host-test-image-cache.yml"

_DPKG_STUB = "#!/bin/bash\necho arm64\n"


def _build(
    tmp_path: Path, *args: str, docker_exit: int = 0
) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Run build-image.sh 24.04 on the stubs, returning the result and each docker argv."""
    calls = tmp_path / "calls"
    docker = f"""#!/bin/bash\nprintf '%s\\n' "$*" >> "{calls}"\nexit {docker_exit}\n"""
    result = run(
        [str(SCRIPT), "24.04", *args],
        stub_bin=fake_bin(tmp_path / "bin", docker=docker, dpkg=_DPKG_STUB),
    )
    return result, calls.read_text().splitlines()


def test_a_plain_build_reads_the_scope_and_never_writes_it(tmp_path: Path) -> None:
    result, (call,) = _build(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "--cache-from type=gha,scope=host-test-image-24.04-arm64" in call
    assert "--cache-to" not in call


def test_a_seed_build_writes_the_scope_and_does_not_tolerate_a_failed_write(
    tmp_path: Path,
) -> None:
    result, (call,) = _build(tmp_path, "--seed")
    assert result.returncode == 0, result.stderr
    assert "--cache-to type=gha,scope=host-test-image-24.04-arm64,mode=min" in call
    assert "ignore-error" not in call


@pytest.mark.parametrize("args", [(), ("--seed",)])
def test_a_failed_build_fails_the_step_without_a_second_build(
    tmp_path: Path, args: tuple[str, ...]
) -> None:
    result, calls = _build(tmp_path, *args, docker_exit=1)
    assert result.returncode == 1
    assert len(calls) == 1


def _build_lines(workflow: Path) -> list[tuple[dict, str]]:
    """Each (job, build-image.sh line) in a workflow."""
    return [
        (job, line)
        for job in yaml_fast.safe_load(workflow.read_text())["jobs"].values()
        for step in job.get("steps", [])
        for line in step.get("run", "").splitlines()
        if "build-image.sh" in line
    ]


def _build_sites(workflow: Path) -> set[tuple[str, str]]:
    """Every (runner label, release) a workflow runs build-image.sh on."""
    sites = set()
    for job, line in _build_lines(workflow):
        for match in re.finditer(r"build-image\.sh \"?([^\s\"]+)", line):
            if match.group(1) != "$RELEASE":
                sites.add((job["runs-on"], match.group(1)))
                continue
            matrix = job["strategy"]["matrix"]
            releases = matrix.get("release") or [
                row["release"] for row in matrix["include"]
            ]
            sites |= {(job["runs-on"], release) for release in releases}
    return sites


def test_the_seeder_covers_every_build_site_on_its_own_runner() -> None:
    sites = _build_sites(CI_WORKFLOW)
    named = {
        ("ubuntu-26.04", "24.04"),
        ("ubuntu-26.04", "26.04"),
        ("ubuntu-24.04-arm", "24.04"),
    }
    assert named <= sites, f"the census lost {sorted(named - sites)}"
    assert _build_sites(SEEDER) == sites


def test_only_the_seeder_writes_the_cache() -> None:
    assert all("--seed" in line for _, line in _build_lines(SEEDER))
    assert not any("--seed" in line for _, line in _build_lines(CI_WORKFLOW))


def test_the_seeder_runs_on_a_dockerfile_change_and_on_a_schedule() -> None:
    workflow = yaml_fast.safe_load(SEEDER.read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["schedule"], "no schedule, so an idle week evicts the cache"
    assert triggers["push"]["branches"] == ["master"]
    assert {
        "hosts/lib/tests/container/Dockerfile",
        "hosts/lib/tests/container/build-image.sh",
    } <= set(triggers["push"]["paths"])
