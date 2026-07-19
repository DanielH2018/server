"""Guard: the Python version is declared once and every copy agrees.

`.python-version` (what uv reads to pick the interpreter) is the canonical fact. The floor in
`pyproject.toml` `requires-python` and every CI `python-version:` pin must match it, so bumping one
copy can't silently leave CI testing a different interpreter than the repo targets (the "same fact
copied into policy code and fixtures" anti-pattern). Fails the instant any copy drifts.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PYTHON_VERSION_FILE = REPO / ".python-version"
PYPROJECT = REPO / "pyproject.toml"
WORKFLOWS = REPO / ".github/workflows"


def _canonical():
    return PYTHON_VERSION_FILE.read_text().strip()


def _requires_python_floor():
    m = re.search(
        r'requires-python\s*=\s*"[>=~ ]*([0-9]+\.[0-9]+)', PYPROJECT.read_text()
    )
    return m.group(1) if m else None


def _workflow_pins():
    return [
        (wf.name, v)
        for wf in sorted(WORKFLOWS.glob("*.yml"))
        for v in re.findall(r'python-version:\s*"([0-9]+\.[0-9]+)"', wf.read_text())
    ]


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
    mismatched = [(wf, v) for wf, v in pins if v != canonical]
    assert not mismatched, (
        f"CI python-version pins disagree with .python-version ({canonical}): {mismatched} — "
        f"every setup-python step must test the interpreter the repo targets"
    )
