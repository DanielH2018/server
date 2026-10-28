"""No test bounds test_session_health.py's wall-clock time (#5916).

A timed run of the file over a serial inner pytest goes red on a loaded fan-out host with no
code change. Host independence is guarded instead by the subprocess test's fence: it asserts
the `prune_worktrees.py` call reached the `uv` stand-in that `_fence_worktree_survey` installs.

Run: uv run pytest .claude/hooks/tests/test_session_health_no_wall_clock_bound.py
"""

import ast
import subprocess
from pathlib import Path

import pytest

from lib.repo_paths import REPO as _REPO

_TESTS_DIR = ".claude/hooks/tests"
_TARGET = "test_session_health.py"

# The `pytest_shard` functions that time a pytest run over the files they are given.
_TIMING_CALLS = {"measure_weights", "record_named_weights"}

_FENCE_TEST = "test_the_hook_can_import_the_worktree_library_when_run_as_a_subprocess"
_FENCE_FIXTURE = "_fence_worktree_survey"


def _tracked_test_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--", f"{_TESTS_DIR}/*.py"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    return [_REPO / line for line in out.splitlines() if line]


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _timing_calls(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        f"{path.relative_to(_REPO)}:{node.lineno} {_called_name(node)}()"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _called_name(node) in _TIMING_CALLS
    ]


def test_no_hooks_test_times_test_session_health_and_the_fence_remains():
    """Verify-by: no test in .claude/hooks/tests/ bounds the file's wall-clock time; the fence stays.

    The census covers every tracked module under the directory, the file itself included, so
    a timing test moved to another module still counts.
    """
    files = _tracked_test_files()
    assert any(p.name == _TARGET for p in files), (
        f"{_TARGET} is not tracked under {_TESTS_DIR}"
    )

    timed = [hit for path in files for hit in _timing_calls(path)]
    assert not timed, (
        f"these tests in {_TESTS_DIR} time a pytest run with a wall-clock measurement, "
        f"which goes red on a loaded host with no code change: {timed}"
    )

    tree = ast.parse((_REPO / _TESTS_DIR / _TARGET).read_text(encoding="utf-8"))
    fence = next(
        (
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == _FENCE_TEST
        ),
        None,
    )
    assert fence is not None, f"{_FENCE_TEST} is gone from {_TARGET}"
    assert _FENCE_FIXTURE in {a.arg for a in fence.args.args}, (
        f"{_FENCE_TEST} no longer takes the {_FENCE_FIXTURE} fixture"
    )
    assert any(isinstance(n, ast.Assert) for n in ast.walk(fence)), (
        f"{_FENCE_TEST} no longer asserts on the fenced calls"
    )


_PRUNE_CALL = "prune_worktrees.py"


def _asserts_prune_call_recorded(fence: ast.FunctionDef) -> bool:
    """Return whether the fence loops over a tuple naming the prune call and asserts `in`.

    Any `assert` in the body is not enough: the subprocess test also asserts on the hook's
    stdout, which says nothing about whether the worktree survey reached the stand-in.
    """
    for loop in ast.walk(fence):
        if not isinstance(loop, ast.For) or not isinstance(loop.iter, ast.Tuple):
            continue
        named = any(
            isinstance(e, ast.Constant)
            and isinstance(e.value, str)
            and _PRUNE_CALL in e.value
            for e in loop.iter.elts
        )
        checks_in = any(
            isinstance(n, ast.Assert)
            and isinstance(n.test, ast.Compare)
            and any(isinstance(op, ast.In) for op in n.test.ops)
            for n in ast.walk(loop)
        )
        if named and checks_in:
            return True
    return False


def _fence_def(source: str) -> ast.FunctionDef:
    return next(
        n
        for n in ast.parse(source).body
        if isinstance(n, ast.FunctionDef) and n.name == _FENCE_TEST
    )


def test_the_fence_asserts_the_prune_call_reached_the_stand_in():
    """The fence test asserts `prune_worktrees.py` is among the calls the stand-in recorded."""
    fence = _fence_def((_REPO / _TESTS_DIR / _TARGET).read_text(encoding="utf-8"))
    assert _asserts_prune_call_recorded(fence), (
        f"{_FENCE_TEST} no longer asserts that the {_PRUNE_CALL} call reached the "
        f"{_FENCE_FIXTURE} stand-in, so nothing guards the survey's host independence"
    )


def _fence_head() -> str:
    """A fence test's opening lines, which the parametrized bodies below complete."""
    return f"def {_FENCE_TEST}({_FENCE_FIXTURE}):\n    assert 'broken' not in out\n"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "    for b in ('curl', 'uv run x/prune_worktrees.py'):\n"
            "        assert b in rec\n",
            True,
        ),
        ("    for b in ('curl', 'journalctl'):\n        assert b in rec\n", False),
        ("", False),
    ],
    ids=["names-and-asserts", "prune-dropped-from-tuple", "loop-deleted"],
)
def test_the_prune_predicate_rejects_a_fence_without_the_prune_assert(body, expected):
    assert _asserts_prune_call_recorded(_fence_def(_fence_head() + body)) is expected
