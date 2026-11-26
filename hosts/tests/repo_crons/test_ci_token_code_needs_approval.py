"""The code a CI job runs with its write-scoped token is on the lander's approval list.

`TOKEN_JOBS` names the jobs. Each one's scripts are read from its steps and each script's
imports from a fresh interpreter, so a script or an import a job gains fails here until the
approval list names it.

Run: uv run pytest hosts/tests/repo_crons/test_ci_token_code_needs_approval.py
"""

import functools
import os
import re
import sys

from _helpers import REPO
from _setup_render import render_component_text
from deploy_tools.land_lib import policy
from lib import proc_testing, yaml_fast

# The jobs whose write-scoped token reaches a step that runs a script from the checkout.
TOKEN_JOBS = {"ci.yml": "prek", "promote.yml": "images"}
_SCRIPT = re.compile(r"\bpython3? (scripts/[\w/]+\.py)")
_LOADED = """
import runpy, sys
from pathlib import Path

root, script = Path.cwd().resolve(), Path(sys.argv[1]).resolve()
sys.path.insert(0, str(script.parent))
runpy.run_path(str(script), run_name="imported")
for module in list(sys.modules.values()):
    source = getattr(module, "__file__", None)
    if source and Path(source).resolve().is_relative_to(root):
        print(Path(source).resolve().relative_to(root).as_posix())
"""


@functools.cache
def run_with_a_write_token() -> frozenset[str]:
    """Each checkout file a `TOKEN_JOBS` step executes while its environment holds a token."""
    ran: set[str] = set()
    for name, job_name in TOKEN_JOBS.items():
        workflow = yaml_fast.safe_load((REPO / ".github/workflows" / name).read_text())
        job = workflow["jobs"][job_name]
        assert "write" in job["permissions"].values(), f"{name}:{job_name}"
        for step in job["steps"]:
            env = {**job.get("env", {}), **step.get("env", {})}
            if not any("github.token" in str(value) for value in env.values()):
                continue
            for script in _SCRIPT.findall(step.get("run", "")):
                loaded = proc_testing.run(
                    [sys.executable, "-c", _LOADED, script],
                    cwd=REPO,
                    env={**os.environ, "PYTHONPATH": env.get("PYTHONPATH", "")},
                    check=True,
                )
                ran.update({script, *loaded.stdout.split()})
    return frozenset(ran)


def approval_list(tmp_path) -> list[str]:
    listed = tmp_path / "approval-paths"
    listed.write_text(
        render_component_text("claude-code", "claude-land-approval-paths.j2")
    )
    return policy.read_approval_paths(str(listed))


def test_code_run_with_a_write_token_is_listed(tmp_path) -> None:
    ran = run_with_a_write_token()
    assert {
        "scripts/dev/tested_tree.py",
        "scripts/deploy_tools/ci_images.py",
        "hosts/lib/ci_verdict.py",
    } <= ran
    entries = approval_list(tmp_path)
    assert sorted(path for path in ran if not policy.listed(path, entries)) == []


def test_an_unlisted_import_of_a_token_step_is_flagged(tmp_path) -> None:
    entries = [p for p in approval_list(tmp_path) if p != "scripts/lib/tree_stack.py"]
    unlisted = [p for p in run_with_a_write_token() if not policy.listed(p, entries)]
    assert unlisted == ["scripts/lib/tree_stack.py"]
