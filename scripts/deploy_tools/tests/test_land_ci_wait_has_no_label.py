"""`wait_master_ci` takes the merge commit alone: nothing passes it a label (#7382).

Run: uv run pytest scripts/deploy_tools/tests/test_land_ci_wait_has_no_label.py
"""

import ast
import inspect
import subprocess

from deploy_tools.land_lib import ci
from lib.repo_paths import REPO

CI_MODULE = "scripts/deploy_tools/land_lib/ci.py"
PRODUCTION_CALLER = "scripts/deploy_tools/land_lib/pipeline.py"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=True, timeout=30
    ).stdout


def _wait_master_ci_calls(path: str) -> list[ast.Call]:
    """Every call in the tracked file `path` to a function named wait_master_ci."""
    return [
        node
        for node in ast.walk(ast.parse((REPO / path).read_text()))
        if isinstance(node, ast.Call)
        and "wait_master_ci"
        in (getattr(node.func, "attr", None), getattr(node.func, "id", None))
    ]


def test_ci_module_never_says_label():
    assert _git("ls-files", "--", CI_MODULE).split() == [CI_MODULE]
    hits = [
        line for line in (REPO / CI_MODULE).read_text().splitlines() if "label" in line
    ]
    assert not hits, hits


def test_wait_master_ci_takes_only_the_landing_and_the_sha():
    assert list(inspect.signature(ci.wait_master_ci).parameters) == ["ln", "sha"]


def test_wait_master_ci_keeps_no_where_or_cannot_cross_arm():
    local_names = set(ci.wait_master_ci.__code__.co_varnames)
    assert not local_names & {"where", "cannot_cross"}, local_names


def test_wait_master_ci_docstring_names_no_stale_retry_caller():
    assert "stale-retry" not in (inspect.getdoc(ci.wait_master_ci) or "")


def test_no_tracked_call_passes_a_third_argument():
    """The labelled tests go with the parameter; pipeline.py is the call this must still find."""
    paths = [
        path
        for path in _git("ls-files", "--", "*.py").split("\n")
        if path and "wait_master_ci" in (REPO / path).read_text(errors="replace")
    ]
    assert PRODUCTION_CALLER in paths
    assert _wait_master_ci_calls(PRODUCTION_CALLER)
    labelled = [
        f"{path}:{call.lineno}"
        for path in paths
        for call in _wait_master_ci_calls(path)
        if len(call.args) + len(call.keywords) > 2
    ]
    assert not labelled, labelled
