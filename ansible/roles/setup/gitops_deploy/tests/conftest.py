"""Shared fixtures for the tests on gitops_deploy.py.

gitops_deploy.py reads its config from the path in GITOPS_DEPLOY_CONFIG at import. This
file points that at the canned `config.env` beside it BEFORE any test module imports the
deployer, so the suite imports the same module in CI and on a host, and never opens the
host's /etc copy (0600, it carries the Discord webhook). The `gitops_deploy` fixture is
that import; `state` is a `DeployerState` over tmp_path, which a test passes to main() in place
of the host's /var/lib/gitops-deploy.

The AST fixtures below remain for the guards that pin a function's shape at the source. One
per module those guards read: `gitops_fn` for the entry module, `deploy_io_fn`, and
`handlers_fn` for `deploy_handlers.py`. `tick` runs main() itself against a scripted checkout
(test_gitops_deploy_main_branches.py).

`settings` is the `Config` a phase takes: `tick_config()` with `repo` pointed at the
scripted checkout. Nothing here patches a module. main() and entrypoint() take their tools,
config and state as arguments (#3744).

Fixtures rather than importable functions: `from conftest import x` resolves to whichever
conftest.py sys.path reached first once the whole repo suite runs, and this repo has three.
pytest resolves a fixture by directory, so it cannot collide. tests/ sits outside files/,
so nothing here is in the role's ship list and nothing here reaches a host.
"""

import ast
import dataclasses
import os
import pathlib
from collections.abc import Callable
from types import ModuleType

import pytest

import deploy_io
import deploy_setup_roles
from _deploy_fakes import ScriptedTick, build_tools


@pytest.fixture(autouse=True)
def _restore_setup_routing():
    """Put back the setup-role routing a test's tick installed, so no later test routes by it.

    Every `main()` installs routing for the rest of the process, and a test that scripts a
    failed derivation installs none at all; a later test on the same worker would inherit it.
    """
    saved = deploy_setup_roles.current_routing()
    yield
    deploy_setup_roles.use_routing(saved)


FILES = pathlib.Path(__file__).resolve().parents[1] / "files"
GITOPS_SRC = FILES / "gitops_deploy.py"
IO_SRC = FILES / "deploy_io.py"
HANDLERS_SRC = FILES / "deploy_handlers.py"

# At import, not in a fixture: a test module's own `import gitops_deploy` runs at collection,
# before any fixture. pytest imports a directory's conftest.py ahead of its test modules.
os.environ["GITOPS_DEPLOY_CONFIG"] = str(pathlib.Path(__file__).with_name("config.env"))


@pytest.fixture(autouse=True)
def service_lock_dir(tmp_path, monkeypatch) -> pathlib.Path:
    """Point the per-service locks at tmp_path, and hand back the directory to read them from.

    Every deploy this suite drives flocks one file per service (ADR-0017). Left at
    /var/lock those would sit beside the real locks, so a test would flock what a live deploy
    holds and block on it. Autouse and directory-wide, because the set of modules that reach a
    deploy function is not closed.

    `_deploy_fakes.locks_taken` reads this directory: a lock file exists only because a deploy
    took it, so the directory IS the record of which locks a call took.
    """
    locks = tmp_path / "service-locks"
    locks.mkdir()
    # `setenv`, not `setattr`: `deploy_locks.lock_dir()` reads the variable per call, the same
    # one `scripts/deploy.sh` honours, so the redirect needs no seam in the module.
    monkeypatch.setenv("HOMELAB_DEPLOY_LOCK_DIR", str(locks))
    return locks


@pytest.fixture(scope="session")
def gitops_deploy() -> ModuleType:
    """The deployer module, imported against the canned config."""
    import gitops_deploy

    assert gitops_deploy.REPO == "/tmp/gitops-test-repo", (
        f"gitops_deploy imported against {gitops_deploy.CONFIG_PATH}, not the canned config"
    )
    return gitops_deploy


@pytest.fixture
def state_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """The directory `state` keeps its markers in, so a test reads what a tick wrote
    (`last_run`, `pending_alerts.json`, the keyed `alerted_shas` marker) under the same
    basenames without touching the host."""
    return tmp_path


@pytest.fixture
def state(state_dir: pathlib.Path) -> deploy_io.DeployerState:
    """The `DeployerState` a test passes to main(), entrypoint() and every phase.

    One object carries every marker path, and `DeployerState.MARKERS` is the only table of
    basenames, so this object is the whole state directory.
    `test_state_dir_repoints_every_state_path_in_the_module` keeps it whole: a path literal
    for the state directory anywhere in `gitops_deploy.py` would escape this object and write
    to /var/lib during a test run.
    """
    return deploy_io.DeployerState(state_dir)


@pytest.fixture(scope="session")
def gitops_src() -> pathlib.Path:
    """The path of gitops_deploy.py, for the two guards that read its raw text."""
    return GITOPS_SRC


@pytest.fixture(scope="session")
def gitops_tree() -> ast.Module:
    return ast.parse(GITOPS_SRC.read_text())


@pytest.fixture(scope="session")
def deploy_io_tree() -> ast.Module:
    """deploy_io.py's parsed source, for the guards that follow a function that moved there."""
    return ast.parse(IO_SRC.read_text())


@pytest.fixture(scope="session")
def deploy_io_fn(
    deploy_io_tree: ast.Module,
) -> Callable[[str, ast.AST | None], ast.FunctionDef]:
    """`deploy_io_fn("deploy_k8s")` is that FunctionDef; a missing name fails."""
    return _fn_finder(deploy_io_tree, "deploy_io.py")


def _fn_finder(default_tree: ast.Module, filename: str):
    """A `fn(name, tree=None)` returning that FunctionDef, asserting rather than returning None.

    `tree` overrides the parsed module for a rejecting half that parses the pre-fix shape
    of a function and asserts the check still flags it.
    """

    def _fn(name: str, tree: ast.AST | None = None) -> ast.FunctionDef:
        fn = next(
            (
                n
                for n in ast.walk(tree if tree is not None else default_tree)
                if isinstance(n, ast.FunctionDef) and n.name == name
            ),
            None,
        )
        assert fn is not None, f"{name}() not found in {filename}"
        return fn

    return _fn


@pytest.fixture(scope="session")
def handlers_tree() -> ast.Module:
    """deploy_handlers.py's parsed source, for the guards that follow the handlers there."""
    return ast.parse(HANDLERS_SRC.read_text())


@pytest.fixture(scope="session")
def handlers_fn(
    handlers_tree: ast.Module,
) -> Callable[[str, ast.AST | None], ast.FunctionDef]:
    """`handlers_fn("handle_k8s")` is that FunctionDef; a missing name fails."""
    return _fn_finder(handlers_tree, "deploy_handlers.py")


@pytest.fixture(scope="session")
def gitops_fn(
    gitops_tree: ast.Module,
) -> Callable[[str, ast.AST | None], ast.FunctionDef]:
    """`gitops_fn("main")` is main()'s FunctionDef; a missing name fails, never returns None."""
    return _fn_finder(gitops_tree, "gitops_deploy.py")


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


@pytest.fixture
def tick(gitops_deploy: ModuleType, state, tmp_path) -> ScriptedTick:
    """Run main() against a scripted checkout.

    git, ansible-playbook, the CI verdict, the health gate, the clock and
    Discord all answer from the ScriptedTick through the `DeployTools` on `tick.tools`, and the
    state files live under `state_dir`. Nothing reaches a shell or the network, and nothing
    patches a module: `deploy_io.deploy_k8s` and `deploy_broad` reach the scripted runner as
    `run=tools.run`.

    Call `gitops_deploy.main(tick.tools, tick.config, state)`. `tick.config` is
    `tick_config()` pointed at the scripted checkout.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    scripted = ScriptedTick(repo)
    scripted.tools = build_tools(scripted)
    scripted.config = dataclasses.replace(gitops_deploy.tick_config(), repo=str(repo))
    scripted.state = state
    return scripted


@pytest.fixture
def settings(tick: ScriptedTick):
    """The `Config` a phase takes: `tick.config`, pointed at the scripted checkout."""
    return tick.config
