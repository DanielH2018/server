"""The drill's `main` reaches each predicate it defines, or the predicate lives elsewhere.

`check` reaches `closure_problems` and `sync_problems`, or neither exists (#8170).
`main` reaches `placeholder_problems` and its `_strings`, through `check` or `build`, or the
predicate lives only in the renderer test (#8254). A census over `bootstrap_drill.py`'s call graph: the
module is parsed, never imported.

Run: uv run pytest scripts/deploy_tools/tests/test_bootstrap_drill_check_wiring.py
"""

import ast
import sys
from pathlib import Path

import pytest

DRILL = Path(__file__).resolve().parents[1] / "bootstrap_drill.py"
RENDERER_TEST = (
    Path(__file__).resolve().parents[2]
    / "renderer"
    / "tests"
    / "test_renderer_bootstrap_documents.py"
)
# `build` writes the fixture; a call there does not reach the verdict `check` prints.
NOT_CHECK = frozenset({"build"})


def _top_level(
    path: Path | None = None,
) -> tuple[dict[str, ast.FunctionDef], dict[str, list[ast.expr]]]:
    tree = ast.parse((path or DRILL).read_text())
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


def _reachable_from_main(skip: frozenset[str] = NOT_CHECK) -> set[str]:
    functions, assigned = _top_level()
    seen: set[str] = set()
    todo: list[ast.AST] = [functions["main"]]
    while todo:
        node = todo.pop()
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Name) or sub.id in seen or sub.id in skip:
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


@pytest.mark.parametrize("helper", ["placeholder_problems", "_strings"])
def test_main_reaches_the_placeholder_check_or_it_lives_in_the_renderer_test(helper):
    if helper in _top_level()[0]:
        assert helper in _reachable_from_main(skip=frozenset()), (
            f"{helper} is defined in bootstrap_drill.py but neither `check` nor `build` "
            "calls it"
        )
    elif helper == "placeholder_problems":
        assert helper in _top_level(RENDERER_TEST)[0], (
            f"{helper} left bootstrap_drill.py but the renderer test does not define it"
        )


_MODULE = """
def wired(items):
    return []


def unwired(items):
    return []


def build(out):
    unwired(out)


def main():
    problems = {"a": lambda: wired([])}
    return problems
"""


def test_a_verdict_main_reaches_is_clean(tmp_path, monkeypatch):
    (tmp_path / "drill.py").write_text(_MODULE)
    monkeypatch.setattr(sys.modules[__name__], "DRILL", tmp_path / "drill.py")
    assert "wired" in _reachable_from_main()


def test_a_verdict_only_build_reaches_is_flagged(tmp_path, monkeypatch):
    (tmp_path / "drill.py").write_text(_MODULE)
    monkeypatch.setattr(sys.modules[__name__], "DRILL", tmp_path / "drill.py")
    assert "unwired" in _top_level()[0]
    assert "unwired" not in _reachable_from_main()
