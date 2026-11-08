"""The host_packages test image: when build-image.sh pulls the pin, builds, or publishes (#7664).

`build-image.sh` runs from a copy of its directory against a stub `docker`, so each test writes
its own image.pins and reads the argv a real run would pass. The census half holds the
publisher's jobs equal to ci.yml's build sites: a site with no published image builds from the
mirror on every run and nothing goes red.

Run: uv run pytest ansible/tests/repo/test_host_test_image.py
"""

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from _helpers import REPO
from lib import yaml_fast
from lib.proc_testing import fake_bin, run

CONTEXT = REPO / "hosts" / "lib" / "tests" / "container"
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
PUBLISHER = REPO / ".github" / "workflows" / "host-test-image.yml"
IMAGE = "ghcr.io/danielh2018/homelab-host-test"
DIGEST = "sha256:" + "ab" * 32
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")

_DPKG_STUB = "#!/bin/bash\necho arm64\n"


def _inputs() -> str:
    return hashlib.sha256((CONTEXT / "Dockerfile").read_bytes()).hexdigest()


def _tag() -> str:
    return f"{IMAGE}:24.04-arm64-{_inputs()[:12]}"


def _build(
    tmp_path: Path,
    *args: str,
    pins: str,
    pullable: tuple[str, ...] = (),
    inspect: str = "missing",
) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Run build-image.sh 24.04 with `pins` as image.pins, returning the result and each docker argv.

    A `docker pull` succeeds only for a ref in `pullable`.
    """
    context = tmp_path / "context"
    context.mkdir()
    for name in ("build-image.sh", "Dockerfile"):
        shutil.copy2(CONTEXT / name, context / name)
    (context / "image.pins").write_text(pins)
    calls = tmp_path / "calls"
    pull_cases = "".join(f'  "pull {ref}") exit 0 ;;\n' for ref in pullable)
    docker = f"""#!/bin/bash
printf '%s\\n' "$*" >> "{calls}"
case "$1 $2" in
{pull_cases}  "pull "*) exit 1 ;;
  "inspect "*) echo "{IMAGE}@{DIGEST}" ;;
  "buildx imagetools")
    [[ "$*" == *--format* ]] && {{ echo {DIGEST}; exit 0; }}
    case "{inspect}" in
      exists) exit 0 ;;
      missing) echo "ERROR: ghcr.io/x: not found" >&2; exit 1 ;;
      *) echo "ERROR: unexpected status code 503" >&2; exit 1 ;;
    esac ;;
esac
"""
    result = run(
        [str(context / "build-image.sh"), "24.04", *args],
        stub_bin=fake_bin(tmp_path / "bin", docker=docker, dpkg=_DPKG_STUB),
    )
    return result, calls.read_text().splitlines()


def _matching_pins() -> str:
    return f"inputs {_inputs()}\n24.04-arm64 {DIGEST}\n"


def _stale_pins() -> str:
    return f"inputs {'0' * 64}\n24.04-arm64 {DIGEST}\n"


def _docker_verbs(calls: list[str]) -> list[str]:
    """Each call's verb, with the inspect that names a pulled digest left out."""
    return [
        " ".join(call.split()[:2] if call.startswith("buildx") else call.split()[:1])
        for call in calls
        if not call.startswith("inspect ")
    ]


def test_a_pin_for_this_dockerfile_pulls_by_digest_and_never_builds(
    tmp_path: Path,
) -> None:
    pinned = f"{IMAGE}@{DIGEST}"
    result, calls = _build(tmp_path, pins=_matching_pins(), pullable=(pinned,))
    assert result.returncode == 0, result.stderr
    assert [c for c in calls if not c.startswith("inspect ")] == [
        f"pull {pinned}",
        f"tag {pinned} homelab-host-test:24.04",
    ]


def test_a_stale_pin_pulls_this_dockerfiles_published_tag_and_warns(
    tmp_path: Path,
) -> None:
    result, calls = _build(tmp_path, pins=_stale_pins(), pullable=(_tag(),))
    assert result.returncode == 0, result.stderr
    assert f"pull {_tag()}" in calls
    assert f"{IMAGE}@{DIGEST}" not in " ".join(c for c in calls if c.startswith("pull"))
    assert "buildx build" not in _docker_verbs(calls)
    assert "image.pins is stale" in result.stdout


def test_a_dockerfile_with_no_published_image_is_built(tmp_path: Path) -> None:
    result, calls = _build(tmp_path, pins=_stale_pins())
    assert result.returncode == 0, result.stderr
    assert _docker_verbs(calls) == ["pull", "buildx build"]
    assert calls[0] == f"pull {_tag()}"
    assert "-t homelab-host-test:24.04" in calls[-1]


def test_a_failed_pinned_pull_falls_back_to_the_tag_then_the_build(
    tmp_path: Path,
) -> None:
    result, calls = _build(tmp_path, pins=_matching_pins())
    assert result.returncode == 0, result.stderr
    assert calls[:2] == [f"pull {IMAGE}@{DIGEST}", f"pull {_tag()}"]
    assert calls[2].startswith("buildx build --load")
    assert "Pulling the pinned" in result.stdout


def test_a_publish_pushes_a_missing_tag_and_prints_its_pin(tmp_path: Path) -> None:
    result, calls = _build(tmp_path, "--publish", pins="inputs -\n")
    assert result.returncode == 0, result.stderr
    assert any(call.startswith("buildx build --load") for call in calls)
    assert f"push {_tag()}" in calls
    assert result.stdout.splitlines()[-1] == f"24.04-arm64 {DIGEST}"


def test_a_publish_never_rebuilds_a_tag_that_exists(tmp_path: Path) -> None:
    result, calls = _build(tmp_path, "--publish", pins="inputs -\n", inspect="exists")
    assert result.returncode == 0, result.stderr
    assert not any(call.startswith(("buildx build", "push")) for call in calls)
    assert result.stdout.splitlines()[-1] == f"24.04-arm64 {DIGEST}"


def test_a_publish_that_cannot_tell_whether_the_tag_exists_pushes_nothing(
    tmp_path: Path,
) -> None:
    result, calls = _build(tmp_path, "--publish", pins="inputs -\n", inspect="error")
    assert result.returncode == 1
    assert not any(call.startswith(("buildx build", "push")) for call in calls)
    assert "503" in result.stderr


_ARCH = {"ubuntu-26.04": "amd64", "ubuntu-24.04-arm": "arm64"}


def _pin_problems(text: str, sites: set[tuple[str, str]]) -> list[str]:
    """Why image.pins `text` does not pin one image by digest per CI build site."""
    pins = dict(
        line.split()
        for line in text.splitlines()
        if line.strip() and not line.startswith("#")
    )
    if "inputs" not in pins:
        return ["no inputs line"]
    if pins["inputs"] != "-" and not re.fullmatch(r"[0-9a-f]{64}", pins["inputs"]):
        return [f"inputs is no Dockerfile sha256: {pins['inputs']}"]
    keys = {f"{release}-{_ARCH[runner]}" for runner, release in sites}
    problems = [f"{key} is no build site" for key in pins.keys() - keys - {"inputs"}]
    problems += [
        f"{key} is not a digest: {value}"
        for key, value in pins.items()
        if key != "inputs" and not DIGEST_RE.fullmatch(value)
    ]
    # `inputs -` is the state before the first publish, when every leg builds.
    if pins["inputs"] != "-":
        problems += [f"{key} has no pin" for key in sorted(keys - pins.keys())]
    return problems


def test_a_complete_pin_file_is_clean() -> None:
    sites = _build_sites(CI_WORKFLOW)
    lines = [f"{r}-{_ARCH[runner]} {DIGEST}" for runner, r in sites]
    assert _pin_problems("\n".join([f"inputs {_inputs()}", *lines]), sites) == []


def test_a_pin_file_missing_a_site_or_holding_a_tag_is_flagged() -> None:
    sites = _build_sites(CI_WORKFLOW)
    text = f"inputs {_inputs()}\n24.04-amd64 {DIGEST}\n26.04-amd64 latest\n"
    assert _pin_problems(text, sites) == [
        "26.04-amd64 is not a digest: latest",
        "24.04-arm64 has no pin",
    ]
    assert _pin_problems("inputs latest\n", sites) == [
        "inputs is no Dockerfile sha256: latest"
    ]


def test_image_pins_pins_each_ci_build_site_by_digest() -> None:
    # Not held equal to the Dockerfile's hash: a Dockerfile PR runs before its image can exist.
    text = (CONTEXT / "image.pins").read_text()
    assert _pin_problems(text, _build_sites(CI_WORKFLOW)) == []


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


def test_the_publisher_covers_every_build_site_on_its_own_runner() -> None:
    sites = _build_sites(CI_WORKFLOW)
    named = {
        ("ubuntu-26.04", "24.04"),
        ("ubuntu-26.04", "26.04"),
        ("ubuntu-24.04-arm", "24.04"),
    }
    assert named <= sites, f"the census lost {sorted(named - sites)}"
    assert _build_sites(PUBLISHER) == sites


def test_only_the_publisher_publishes_and_only_from_master() -> None:
    assert all("--publish" in line for _, line in _build_lines(PUBLISHER))
    assert not any("--publish" in line for _, line in _build_lines(CI_WORKFLOW))
    jobs = yaml_fast.safe_load(PUBLISHER.read_text())["jobs"].values()
    assert all(job["if"] == "github.ref == 'refs/heads/master'" for job in jobs)


@pytest.mark.parametrize("job", ["host_packages", "host_tools_arm64"])
def test_each_ci_leg_can_read_the_published_image(job: str) -> None:
    ci_job = yaml_fast.safe_load(CI_WORKFLOW.read_text())["jobs"][job]
    assert ci_job["permissions"].get("packages") == "read"
    (step,) = [
        step for step in ci_job["steps"] if "build-image.sh" in step.get("run", "")
    ]
    assert step["env"]["GHCR_TOKEN"] == "${{ github.token }}"


def test_the_publisher_runs_on_a_dockerfile_change_and_on_a_schedule() -> None:
    workflow = yaml_fast.safe_load(PUBLISHER.read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["schedule"], "no schedule, so a deleted tag is never republished"
    assert triggers["push"]["branches"] == ["master"]
    assert {
        "hosts/lib/tests/container/Dockerfile",
        "hosts/lib/tests/container/build-image.sh",
    } <= set(triggers["push"]["paths"])
