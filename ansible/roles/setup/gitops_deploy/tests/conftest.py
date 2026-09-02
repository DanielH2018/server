"""Shared fixtures for the tests on gitops_deploy.py.

gitops_deploy.py reads its config from the path in GITOPS_DEPLOY_CONFIG at import. This
file points that at the canned `config.env` beside it BEFORE any test module imports the
deployer, so the suite imports the same module in CI and on a host, and never opens the
host's /etc copy (0600, it carries the Discord webhook). The `gitops_deploy` fixture is
that import; `state_dir` repoints every /var/lib/gitops-deploy marker at tmp_path.

The AST fixtures below remain for the invariants that live inside main(), which shells out
to git and GitHub and is pinned at the source instead (test_gitops_deploy_main_guards.py).

Fixtures rather than importable functions: `from conftest import x` resolves to whichever
conftest.py sys.path reached first once the whole repo suite runs, and this repo has three.
pytest resolves a fixture by directory, so it cannot collide. tests/ sits outside files/,
so nothing here is in the role's ship list and nothing here reaches a host.
"""

import ast
import os
import pathlib
from collections.abc import Callable
from types import ModuleType

import pytest

GITOPS_SRC = pathlib.Path(__file__).resolve().parents[1] / "files" / "gitops_deploy.py"
STATE_PREFIX = "/var/lib/gitops-deploy/"

# At import, not in a fixture: a test module's own `import gitops_deploy` runs at collection,
# before any fixture. pytest imports a directory's conftest.py ahead of its test modules.
os.environ["GITOPS_DEPLOY_CONFIG"] = str(pathlib.Path(__file__).with_name("config.env"))


@pytest.fixture(scope="session")
def gitops_deploy() -> ModuleType:
    """The deployer module, imported against the canned config."""
    import gitops_deploy

    assert gitops_deploy.REPO == "/tmp/gitops-test-repo", (
        f"gitops_deploy imported against {gitops_deploy.CONFIG_PATH}, not the canned config"
    )
    return gitops_deploy


@pytest.fixture
def state_dir(
    gitops_deploy: ModuleType, monkeypatch, tmp_path: pathlib.Path
) -> pathlib.Path:
    """tmp_path, with every module constant naming a /var/lib/gitops-deploy path repointed
    into it under the same basename, so a test reads what a tick wrote (`last_run`,
    `pending_alerts.json`, the per-channel `*_alerted_sha` markers) without touching the host."""
    markers = {
        name: value
        for name, value in vars(gitops_deploy).items()
        if isinstance(value, str) and value.startswith(STATE_PREFIX)
    }
    assert "LAST_RUN" in markers and "PENDING_ALERTS_FILE" in markers
    for name, value in markers.items():
        monkeypatch.setattr(
            gitops_deploy, name, str(tmp_path / value.removeprefix(STATE_PREFIX))
        )
    return tmp_path


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
