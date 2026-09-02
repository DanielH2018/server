"""Shared fixtures for the AST guards on gitops_deploy.py.

gitops_deploy.py cannot be imported in CI (module-level `C = cfg()` reads /etc config that
does not exist there), so the test_gitops_deploy_*.py files pin its invariants against the
parsed source. Each of them used to carry its own copy of the same four helpers.

Fixtures rather than importable functions: `from conftest import x` resolves to whichever
conftest.py sys.path reached first once the whole repo suite runs, and this repo has three.
pytest resolves a fixture by directory, so it cannot collide. conftest.py is also exempt from
the role's ship list (`test_gitops_deploy_ship_list.py`), so nothing here reaches a host.
"""

import ast
import pathlib
from collections.abc import Callable

import pytest

GITOPS_SRC = pathlib.Path(__file__).with_name("gitops_deploy.py")


@pytest.fixture(scope="session")
def gitops_src() -> pathlib.Path:
    """The path of gitops_deploy.py, for the two guards that read its raw text."""
    return GITOPS_SRC


@pytest.fixture(scope="session")
def gitops_tree() -> ast.Module:
    return ast.parse(GITOPS_SRC.read_text())


@pytest.fixture(scope="session")
def gitops_fn(gitops_tree: ast.Module) -> Callable[[str], ast.FunctionDef]:
    """`gitops_fn("main")` is main()'s FunctionDef; a missing name fails, never returns None."""

    def _fn(name: str) -> ast.FunctionDef:
        fn = next(
            (
                n
                for n in ast.walk(gitops_tree)
                if isinstance(n, ast.FunctionDef) and n.name == name
            ),
            None,
        )
        assert fn is not None, f"{name}() not found in gitops_deploy.py"
        return fn

    return _fn


@pytest.fixture(scope="session")
def ast_calls() -> Callable[[ast.AST, str], bool]:
    """Whether `node` contains a call to `fn_name`, as a bare name or an attribute."""

    def _calls(node: ast.AST, fn_name: str) -> bool:
        return any(
            isinstance(c, ast.Call)
            and (
                (isinstance(c.func, ast.Name) and c.func.id == fn_name)
                or (isinstance(c.func, ast.Attribute) and c.func.attr == fn_name)
            )
            for c in ast.walk(node)
        )

    return _calls


@pytest.fixture(scope="session")
def str_constants() -> Callable[[ast.AST], set[str]]:
    """Every string literal under `node`."""

    def _str_constants(node: ast.AST) -> set[str]:
        return {
            c.value
            for c in ast.walk(node)
            if isinstance(c, ast.Constant) and isinstance(c.value, str)
        }

    return _str_constants
