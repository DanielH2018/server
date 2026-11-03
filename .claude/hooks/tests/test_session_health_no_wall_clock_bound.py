"""test_session_health.py stays host-independent through its fence, not a wall-clock bound (#5916).

A timed run of the file over a serial inner pytest goes red on a loaded fan-out host with no
code change. Host independence is guarded instead by the subprocess test's fence: it asserts
the `prune_worktrees.py` call reached the `uv` stand-in that `_fence_worktree_survey` installs.

Run: uv run pytest .claude/hooks/tests/test_session_health_no_wall_clock_bound.py
"""

import ast

import pytest

from lib.repo_paths import REPO as _REPO

_TESTS_DIR = ".claude/hooks/tests"
_TARGET = "test_session_health.py"

_FENCE_TEST = "test_the_hook_can_import_the_worktree_library_when_run_as_a_subprocess"
_FENCE_FIXTURE = "_fence_worktree_survey"

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
