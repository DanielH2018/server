"""The `::error` annotation CI's gate prints when it refuses, run from the gate's own script.

`test_ci_skip_proven_master.py` owns which results the gate accepts; this module owns what a
red gate says about them.
"""

import subprocess

from _helpers import REPO
from lib import yaml_fast

CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
# Each env name the gate's script reads for a dependency's result.
RESULTS = (
    "HOOKS",
    "PR_GATES",
    "PYTEST_PLAN",
    "PYTEST",
    "LOCKFILE",
    "FLUX_ROUNDTRIP",
    "SOPS_TYPE_PROOF",
    "HOST_PACKAGES_BUILD",
    "HOST_PACKAGES",
    "HOST_PACKAGES_BOOTSTRAP",
    "HOST_TOOLS_ARM64",
)


def _gate(**env: str) -> tuple[int, list[str]]:
    jobs = yaml_fast.safe_load(CI_WORKFLOW.read_text())["jobs"]
    check = next(s for s in jobs["prek"]["steps"] if "HOOKS" in (s.get("env") or {}))
    full = {name: "success" for name in RESULTS}
    full.update({"PROVE": "skipped", "PATH": "/usr/bin:/bin"}, **env)
    run = subprocess.run(
        ["bash", "-c", check["run"]],
        env=full,
        timeout=30,
        capture_output=True,
        text=True,
    )
    return run.returncode, [
        line for line in run.stdout.splitlines() if line.startswith("::error")
    ]


def test_a_red_gate_annotates_only_the_halves_that_failed():
    result = _gate(
        PYTEST="failure",
        HOST_PACKAGES="cancelled",
        HOST_TOOLS_ARM64="skipped",
        ARM64_TOOLS="false",
    )

    assert result == (
        1,
        ["::error title=gate::2 halves failed: pytest=failure host-packages=cancelled"],
    )


def test_an_unknown_prove_result_is_annotated():
    assert _gate(PROVE="neutral") == (
        1,
        [
            "::error title=gate::prove's result is 'neutral', not success, skipped, cancelled or failure"
        ],
    )
