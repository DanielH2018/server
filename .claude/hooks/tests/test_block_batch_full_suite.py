"""`block-batch-full-suite` refuses a whole-suite pytest run in a fan-out batch, and only there.

Run: uv run pytest .claude/hooks/tests/test_block_batch_full_suite.py
"""

import importlib.util
import os
import sys

import pytest

_HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _HOOKS)  # the arm imports _hook_common

_spec = importlib.util.spec_from_file_location(
    "block_batch_full_suite", os.path.join(_HOOKS, "block-batch-full-suite.py")
)
assert _spec and _spec.loader
arm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(arm)


@pytest.fixture
def batch(tmp_path, monkeypatch):
    """A fan-out batch worktree with one small and one large test directory in it."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    root = tmp_path / ".claude" / "worktrees" / "fanout-5667"
    for name, count in (("small/tests", 3), ("big/tests", arm.MAX_MODULES + 1)):
        directory = root / name
        directory.mkdir(parents=True)
        for n in range(count):
            (directory / f"test_m{n}.py").write_text("")
    return root


def _decide(command, cwd):
    return arm.decision({"tool_input": {"command": command}, "cwd": str(cwd)})


@pytest.mark.parametrize(
    "command",
    [
        "uv run pytest",
        "uv run pytest -q -n auto",
        "timeout 590 uv run pytest -q -p no:randomly 2>&1 | tail -25",
        "uv run python -m pytest -x",
        "PYTEST_ADDOPTS=-q uv run pytest",
        "uv run pytest -k census",
        "git add -A && uv run pytest -q",
    ],
)
def test_a_run_naming_no_path_is_denied_in_a_batch(batch, command):
    verdict = _decide(command, batch)
    assert verdict is not None and verdict[0] == "deny"
    assert "CI runs it on the PR" in verdict[1]


def test_a_run_naming_more_than_the_cap_of_modules_is_denied(batch):
    verdict = _decide("uv run pytest small/tests big/tests", batch)
    assert verdict is not None and verdict[0] == "deny"
    assert f"selects {arm.MAX_MODULES + 4} test modules" in verdict[1]


@pytest.mark.parametrize(
    "command",
    [
        "uv run pytest small/tests",
        "uv run pytest -q -n auto small/tests/test_m0.py",
        "uv run pytest small/tests/test_m1.py::test_x -k thing",
        "uv run pytest --collect-only -q",
        "grep -rn pytest small/tests",
        "uv run ruff check .",
    ],
)
def test_a_targeted_run_or_another_command_is_allowed_in_a_batch(batch, command):
    assert _decide(command, batch) is None


def test_a_whole_suite_run_outside_a_batch_is_allowed(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    other = tmp_path / ".claude" / "worktrees" / "gitops-tick-metrics"
    other.mkdir(parents=True)
    assert _decide("uv run pytest -q", other) is None


def test_the_agent_users_batch_and_the_project_dir_both_count(tmp_path, monkeypatch):
    agent = tmp_path / ".claude" / "worktrees" / "claude+fanout-12"
    agent.mkdir(parents=True)
    assert _decide("uv run pytest", agent) is not None
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(agent))
    assert _decide("uv run pytest", tmp_path) is not None
