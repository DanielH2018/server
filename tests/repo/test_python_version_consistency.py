"""Guard: the Python version is declared once and every copy agrees.

`.python-version` (what uv reads to pick the interpreter) is the canonical fact. The floor in
`pyproject.toml` `requires-python` and every CI `python-version:` pin must match it, so bumping one
copy can't silently leave CI testing a different interpreter than the repo targets (the "same fact
copied into policy code and fixtures" anti-pattern). Fails the instant any copy drifts.

The comparison is on major.minor. `.python-version` names the minor the repo targets and stays
two-part; a workflow pin is three-part, because a two-part `python-version:` has
setup-python resolve the patch from whatever the runner's toolcache holds that day, and only a
full literal moves through Renovate. A workflow pin on a different minor still fails here.
"""

import re
import tomllib

from lib import yaml_fast
from _helpers import REPO


PYTHON_VERSION_FILE = REPO / ".python-version"
PYPROJECT = REPO / "pyproject.toml"
WORKFLOWS = REPO / ".github/workflows"
ACTIONS = REPO / ".github/actions"


def _canonical():
    return PYTHON_VERSION_FILE.read_text().strip()


def _requires_python_floor():
    spec = tomllib.loads(PYPROJECT.read_text())["project"].get("requires-python", "")
    m = re.match(r"[>=~ ]*([0-9]+\.[0-9]+)", spec)
    return m.group(1) if m else None


def workflow_pins(name: str, text: str) -> list[tuple[str, str]]:
    """(file, pin) for every setup step's `python-version:` in one parsed workflow or action.

    A workflow's steps sit under `jobs.<id>.steps`, a composite action's under `runs.steps`;
    the CI pin lives in `.github/actions/setup/action.yml` since #3733, so both are read.

    Read as YAML rather than matched as a quoted string (#3663): an unquoted
    `python-version: 3.14` parses as the float 3.14, which the old pattern skipped and this
    reads as the two-part pin it is.
    """
    doc = yaml_fast.safe_load(text)
    step_lists = [job.get("steps") or [] for job in (doc.get("jobs") or {}).values()]
    step_lists.append((doc.get("runs") or {}).get("steps") or [])
    return [
        (name, str(step["with"]["python-version"]))
        for steps in step_lists
        for step in steps
        if "python-version" in (step.get("with") or {})
    ]


def _workflow_pins():
    files = sorted(WORKFLOWS.glob("*.yml")) + sorted(ACTIONS.glob("*/action.yml"))
    return [
        pin
        for path in files
        for pin in workflow_pins(path.relative_to(REPO).as_posix(), path.read_text())
    ]


def _minor(version: str) -> str:
    return ".".join(version.split(".")[:2])


def test_pyproject_floor_matches_python_version_file():
    canonical = _canonical()
    floor = _requires_python_floor()
    assert floor is not None, "could not parse requires-python from pyproject.toml"
    assert floor == canonical, (
        f"pyproject.toml requires-python floor {floor} != .python-version {canonical} — "
        f"the repo targets one interpreter; bump both together"
    )


def test_ci_workflows_pin_the_canonical_python():
    canonical = _canonical()
    pins = _workflow_pins()
    assert pins, (
        "no python-version pins found in .github/workflows — regex or layout changed"
    )
    mismatched = [(wf, v) for wf, v in pins if _minor(v) != _minor(canonical)]
    assert not mismatched, (
        f"CI python-version pins disagree with .python-version ({canonical}): {mismatched} — "
        f"every setup-python step must test the interpreter the repo targets"
    )


def test_ci_workflows_pin_a_full_patch_release():
    """The rejecting half of the three-part rule: a two-part `python-version: "3.14"` floats on the toolcache."""
    pins = _workflow_pins()
    assert pins, (
        "no python-version pins found in .github/workflows — regex or layout changed"
    )
    short = [(wf, v) for wf, v in pins if v.count(".") < 2]
    assert not short, (
        f"two-part python-version pins {short} — setup-python resolves the patch from the "
        f"runner's toolcache that day; pin the full release so it moves only through Renovate"
    )


def test_an_unquoted_two_part_pin_is_read_as_one():
    """The case the quoted-string pattern skipped: YAML reads `3.14` bare as a float."""
    text = "jobs:\n  t:\n    steps:\n      - uses: actions/setup-python@v6\n        with:\n          python-version: 3.14\n"
    assert workflow_pins("ci.yml", text) == [("ci.yml", "3.14")]


def test_a_composite_actions_pin_is_read():
    """The CI pin's home since #3733; a workflow-only reader would find nothing there."""
    text = 'runs:\n  using: composite\n  steps:\n    - uses: actions/setup-python@v6\n      with:\n        python-version: "3.14.7"\n'
    assert workflow_pins("action.yml", text) == [("action.yml", "3.14.7")]


K8S_ROLES = REPO / "k8s/roles"
_PYTHON_IMAGE_PIN = re.compile(r"^(\w+_image):\s*['\"]?python:(\d+\.\d+)", re.MULTILINE)
# A role whose pin the census must find, so a regex or layout change cannot pass over nothing.
KNOWN_PYTHON_IMAGE_PINS = frozenset({"monitor_bridge_k8s_image", "flux_job_image"})


def off_version_pins(text: str, canonical: str) -> list[tuple[str, str]]:
    """(variable, minor) for each `*_image: python:X.Y-…` pin whose minor differs from `canonical`.

    `canonical` is `.python-version` as written; the comparison is on major.minor.
    """
    return [
        (var, minor)
        for var, minor in _PYTHON_IMAGE_PIN.findall(text)
        if minor != _minor(canonical)
    ]


def _k8s_defaults() -> dict[str, str]:
    return {
        path.relative_to(REPO).as_posix(): path.read_text()
        for path in sorted(K8S_ROLES.glob("*/defaults/main.yml"))
    }


def test_k8s_python_image_pins_match_the_python_version_file():
    """Role `files/` code runs in these images and CI tests it on `.python-version` (#8532)."""
    canonical = _canonical()
    defaults = _k8s_defaults()
    found = {
        var for text in defaults.values() for var, _ in _PYTHON_IMAGE_PIN.findall(text)
    }
    missing = KNOWN_PYTHON_IMAGE_PINS - found
    assert not missing, f"census found no python image pin named {sorted(missing)}"
    mismatched = [
        (path, pin)
        for path, text in defaults.items()
        for pin in off_version_pins(text, canonical)
    ]
    assert not mismatched, (
        f"k8s python image pins disagree with .python-version ({canonical}): {mismatched}. "
        f"The pods would run a different interpreter than CI tests their code on."
    )


_PIN_314 = "monitor_bridge_k8s_image: python:3.14-alpine@sha256:" + "0" * 64 + "\n"


def test_an_off_version_python_image_pin_is_flagged():
    assert off_version_pins(_PIN_314, "3.15") == [("monitor_bridge_k8s_image", "3.14")]


def test_a_matching_python_image_pin_is_clean():
    assert off_version_pins(_PIN_314, "3.14.6") == []
    assert off_version_pins("seed_volume_image: alpine:3.24\n", "3.15") == []
