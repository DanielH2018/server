"""The drill's `check` reaches `closure_problems` and `sync_problems`, or neither exists (#8170).

A census over `bootstrap_drill.py`'s call graph, so it holds whether the fix wires both
verdicts into `check` or deletes them: the module is parsed, never imported.

Run: uv run pytest scripts/deploy_tools/tests/test_bootstrap_drill_check_wiring.py
"""

import ast
from pathlib import Path

import pytest

DRILL = Path(__file__).resolve().parents[1] / "bootstrap_drill.py"
# `build` writes the fixture; a call there does not reach the verdict `check` prints.
NOT_CHECK = frozenset({"build"})


def _top_level() -> tuple[dict[str, ast.FunctionDef], dict[str, list[ast.expr]]]:
    tree = ast.parse(DRILL.read_text())
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    assigned: dict[str, list[ast.expr]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned.setdefault(target.id, []).append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                assigned.setdefault(node.target.id, []).append(node.value)
    return functions, assigned


def _reachable_from_main() -> set[str]:
    functions, assigned = _top_level()
    seen: set[str] = set()
    todo: list[ast.AST] = [functions["main"]]
    while todo:
        node = todo.pop()
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Name) or sub.id in seen or sub.id in NOT_CHECK:
                continue
            if sub.id in functions:
                seen.add(sub.id)
                todo.append(functions[sub.id])
            elif sub.id in assigned:
                seen.add(sub.id)
                todo.extend(assigned[sub.id])
    return seen


@pytest.mark.parametrize("verdict", ["closure_problems", "sync_problems"])
def test_check_reaches_the_verdict_or_it_is_deleted(verdict):
    functions, _ = _top_level()
    if verdict not in functions:
        return
    assert verdict in _reachable_from_main(), (
        f"{verdict} is defined in bootstrap_drill.py but `check` never calls it"
    )
