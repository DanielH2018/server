"""`test_probe_usage_and_config_errors.py` pins the `--since 3x` exit code once (#4475).

`rc in (USAGE_ERROR, FAILED)` is implied by `rc == USAGE_ERROR`, so a second test asserting the
weaker form fails only when the stronger one does. This reads the file's syntax tree: every
test function that runs `--since 3x` and asserts on the exit code `rc` is counted, and the one
left must compare `rc == USAGE_ERROR`. A test that asserts on stdout instead is not counted.

Run: uv run pytest scripts/diagnostics/tests/test_probe_bad_since_census.py
"""

import ast
from pathlib import Path

TARGET = Path(__file__).with_name("test_probe_usage_and_config_errors.py")


def _uses_bad_since(func: ast.FunctionDef) -> bool:
    return any(
        isinstance(node, ast.Constant) and node.value == "3x" for node in ast.walk(func)
    )


def _rc_asserts(func: ast.FunctionDef) -> list[ast.Assert]:
    return [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Assert)
        and any(
            isinstance(name, ast.Name) and name.id == "rc"
            for name in ast.walk(node.test)
        )
    ]


def test_one_bad_since_exit_code_test_asserting_usage_error():
    tree = ast.parse(TARGET.read_text())
    tests = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name.startswith("test_")
        and _uses_bad_since(node)
        and _rc_asserts(node)
    ]
    assert [t.name for t in tests] == ["test_bad_since_exits_usage_error"]
    (only,) = _rc_asserts(tests[0])
    test = only.test
    assert isinstance(test, ast.Compare)
    assert isinstance(test.left, ast.Name) and test.left.id == "rc"
    assert len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)
    (right,) = test.comparators
    assert isinstance(right, ast.Name) and right.id == "USAGE_ERROR"
