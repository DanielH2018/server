"""The pytest-tools action installs age and apt-utils from apt once per runner image version.

ci.yml's pytest legs and the nightly sweep both call the action. A leg copies the binaries apt-get installed into `$RUNNER_TEMP/pinned/apt-tools`, and a later
leg installs those copies instead of running apt-get (#8684). The copies link against the
image's own libraries, so the cache key carries the image version, and apt-get stays as the
branch a leg takes when the copies are absent or do not run. No step runs `sudo`, so the
action runs unchanged in a runner pod with no root (docs/ci-throughput.md, Phase C).

Run: uv run pytest tests/repo/test_ci_pytest_apt_tools_cache.py
"""

import copy

import pytest
from _helpers import REPO
from lib import yaml_fast

ACTION = REPO / ".github" / "actions" / "pytest-tools" / "action.yml"
CACHE_STEP = "Cache the age and apt-utils binaries"
INSTALL_STEP = "Install sops, age and apt-utils for the tests"
IMAGE_VERSION = "steps.image.outputs.version"


def _step(workflow: dict, name: str) -> dict:
    return next(s for s in workflow["runs"]["steps"] if s.get("name") == name)


def apt_runs_only_without_working_copies(run: str) -> bool:
    """Whether every apt-get line sits in the `else` of the check that the copies run."""
    in_else, guarded = False, []
    for line in run.splitlines():
        text = line.strip()
        if text.startswith("if cached_apt_tools_run"):
            in_else = False
        elif text == "else":
            in_else = True
        elif text == "fi":
            in_else = False
        elif "apt-get" in text:
            guarded.append(in_else)
    return bool(guarded) and all(guarded)


def problems(workflow: dict) -> list[str]:
    found = []
    cache = _step(workflow, CACHE_STEP)
    if IMAGE_VERSION not in str(cache["with"]["key"]):
        found.append("the cache key does not carry the runner image version")
    if IMAGE_VERSION not in str(cache.get("if", "")):
        found.append("the cache step runs on a runner that names no image version")
    if not apt_runs_only_without_working_copies(_step(workflow, INSTALL_STEP)["run"]):
        found.append("apt-get runs whether or not the cached copies work")
    if any("sudo" in str(s.get("run", "")) for s in workflow["runs"]["steps"]):
        found.append("a step runs sudo, which a runner pod under `restricted` cannot")
    return found


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml_fast.safe_load(ACTION.read_text(encoding="utf-8"))


def test_the_action_is_clean(workflow):
    assert problems(workflow) == []


def test_a_key_without_the_image_version_is_flagged(workflow):
    broken = copy.deepcopy(workflow)
    _step(broken, CACHE_STEP)["with"]["key"] = "apt-tools-${{ runner.os }}"
    assert problems(broken) == ["the cache key does not carry the runner image version"]


def test_an_apt_get_outside_the_fallback_branch_is_flagged(workflow):
    broken = copy.deepcopy(workflow)
    step = _step(broken, INSTALL_STEP)
    step["run"] = step["run"].replace("if cached_apt_tools_run; then", "if true; then")
    step["run"] += "apt-get install -y age\n"
    assert problems(broken) == ["apt-get runs whether or not the cached copies work"]


def test_a_sudo_apt_get_update_is_flagged(workflow):
    broken = copy.deepcopy(workflow)
    step = _step(broken, INSTALL_STEP)
    private_update = 'apt-get -q "${private[@]}" update'
    assert private_update in step["run"]
    step["run"] = step["run"].replace(private_update, "sudo apt-get update -q")
    assert problems(broken) == [
        "a step runs sudo, which a runner pod under `restricted` cannot"
    ]
