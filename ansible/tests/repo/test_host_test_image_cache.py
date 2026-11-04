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

# Appends one line per call to $CALLS, and fails a call carrying --cache-from when $FAIL_CACHED is set.
_DOCKER_STUB = """#!/bin/bash
printf '%s\\n' "$*" >> "$CALLS"
if [ -n "$FAIL_CACHED" ] && [[ " $* " == *" --cache-from "* ]]; then exit 1; fi
if [ -n "$FAIL_ALL" ]; then exit 1; fi
"""
_DPKG_STUB = "#!/bin/bash\necho arm64\n"


def _build(
    tmp_path: Path, event: str, **env: str
) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Run build-image.sh 24.04 on the stubs, returning the result and each docker argv."""
    calls = tmp_path / "calls"
    result = run(
        [str(SCRIPT), "24.04"],
        env={
            "PATH": "/usr/bin:/bin",
            "CALLS": str(calls),
            "GITHUB_EVENT_NAME": event,
            "FAIL_CACHED": "",
            "FAIL_ALL": "",
            **env,
        },
        stub_bin=fake_bin(tmp_path / "bin", docker=_DOCKER_STUB, dpkg=_DPKG_STUB),
    )
    return result, calls.read_text().splitlines()


def test_a_pull_request_reads_the_scope_and_never_writes_it(tmp_path: Path) -> None:
    result, (call,) = _build(tmp_path, "pull_request")
    assert result.returncode == 0, result.stderr
    assert "--cache-from type=gha,scope=host-test-image-24.04-arm64" in call
    assert "--cache-to" not in call


@pytest.mark.parametrize("event", ["schedule", "push", "workflow_dispatch"])
def test_a_run_off_a_pull_request_writes_the_scope_and_tolerates_a_refusal(
    tmp_path: Path, event: str
) -> None:
    result, (call,) = _build(tmp_path, event)
    assert result.returncode == 0, result.stderr
    assert (
        "--cache-to type=gha,scope=host-test-image-24.04-arm64,mode=min,ignore-error=true"
        in call
    )


def test_a_failed_cached_build_runs_again_without_the_cache(tmp_path: Path) -> None:
    result, (cached, plain) = _build(tmp_path, "pull_request", FAIL_CACHED="1")
    assert result.returncode == 0, result.stderr
    assert "--cache-from" in cached
    assert "--cache-from" not in plain
    assert "-t homelab-host-test:24.04" in plain


def test_a_build_that_fails_without_the_cache_fails_the_step(tmp_path: Path) -> None:
    result, calls = _build(tmp_path, "pull_request", FAIL_ALL="1")
    assert result.returncode != 0
    assert len(calls) == 2


def _build_sites(workflow: Path) -> set[tuple[str, str]]:
    """Every (runner label, release) a workflow runs build-image.sh on."""
    sites = set()
    for job in yaml_fast.safe_load(workflow.read_text())["jobs"].values():
        for step in job.get("steps", []):
            match = re.search(r"build-image\.sh \"?([^\s\"]+)", step.get("run", ""))
            if not match:
                continue
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


def test_the_seeder_runs_on_a_dockerfile_change_and_on_a_schedule() -> None:
    workflow = yaml_fast.safe_load(SEEDER.read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["schedule"], "no schedule, so an idle week evicts the cache"
    assert triggers["push"]["branches"] == ["master"]
    assert {
        "hosts/lib/tests/container/Dockerfile",
        "hosts/lib/tests/container/build-image.sh",
    } <= set(triggers["push"]["paths"])
