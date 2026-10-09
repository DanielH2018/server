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


def _canonical():
    return PYTHON_VERSION_FILE.read_text().strip()


def _requires_python_floor():
    spec = tomllib.loads(PYPROJECT.read_text())["project"].get("requires-python", "")
    m = re.match(r"[>=~ ]*([0-9]+\.[0-9]+)", spec)
    return m.group(1) if m else None


def workflow_pins(name: str, text: str) -> list[tuple[str, str]]:
    """(workflow, pin) for every setup step's `python-version:` in one parsed workflow.

    Read as YAML rather than matched as a quoted string (#3663): an unquoted
    `python-version: 3.14` parses as the float 3.14, which the old pattern skipped and this
    reads as the two-part pin it is.
    """
    return [
        (name, str(step["with"]["python-version"]))
        for job in (yaml_fast.safe_load(text).get("jobs") or {}).values()
        for step in job.get("steps") or []
        if "python-version" in (step.get("with") or {})
    ]


def _workflow_pins():
    return [
        pin
        for wf in sorted(WORKFLOWS.glob("*.yml"))
        for pin in workflow_pins(wf.name, wf.read_text())
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
