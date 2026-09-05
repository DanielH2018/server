"""Shared fixtures for the tests on gitops_deploy.py.

gitops_deploy.py reads its config from the path in GITOPS_DEPLOY_CONFIG at import. This
file points that at the canned `config.env` beside it BEFORE any test module imports the
deployer, so the suite imports the same module in CI and on a host, and never opens the
host's /etc copy (0600, it carries the Discord webhook). The `gitops_deploy` fixture is
that import; `state_dir` repoints every /var/lib/gitops-deploy marker at tmp_path.

The AST fixtures below remain for the guards that pin a function's shape at the source
(test_staging_gate_cannot_break_prod.py, test_gitops_deploy_timeout_budgets.py); `tick` runs
main() itself against a scripted checkout (test_gitops_deploy_main_branches.py).

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

import deploy_io
from _deploy_fakes import ScriptedTick, build_tools

FILES = pathlib.Path(__file__).resolve().parents[1] / "files"
GITOPS_SRC = FILES / "gitops_deploy.py"
IO_SRC = FILES / "deploy_io.py"
STATE_PREFIX = "/var/lib/gitops-deploy/"
# What the `tick` fixture arms the staging gate over. The production literal stays in
# gitops_deploy.py, where scripts/docs/gen_doc_fragments.py reads it; this is only what puts the
# scripted k8s service in scope so the real consult_staging has something to gate.
STAGING_SUBSET = frozenset({"sonarr"})

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
    # The constants above are the literals the module declares; STATE is what its code reads and
    # writes them through, so it has to be repointed too or a test would write to /var/lib.
    # DeployerState derives the same basenames, which test_deployer_state.py pins against the
    # constants.
    monkeypatch.setattr(gitops_deploy, "STATE", deploy_io.DeployerState(tmp_path))
    return tmp_path


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
    """`deploy_io_fn("run_staging_scripts")` is that FunctionDef; a missing name fails."""
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
def tick(gitops_deploy: ModuleType, monkeypatch, state_dir, tmp_path) -> ScriptedTick:
    """Run main() against a scripted checkout.

    git, ansible-playbook, the CI verdict, the health gate, the staging scripts, the clock and
    Discord all answer from the ScriptedTick through the `DeployTools` on `tick.tools`, and the
    state files live under `state_dir`. Nothing reaches a shell or the network.

    Call `gitops_deploy.main(tick.tools)`; the fixture injects nothing on its own.

    The ONE remaining module patch is `deploy_io.run`. `deploy_io.deploy`, `deploy_k8s` and
    `deploy_broad` build the `ansible-playbook` argv the suite asserts on and reach `run`
    qualified, so faking them here would retire that assertion; threading a runner into the
    three of them costs deploy_io.py more lines than its entry in
    ansible/tests/repo/module_length_allowlist.txt allows, and lands with the split that lowers
    it.

    The staging gate is ARMED here, and `STAGING_SUBSET` widened to cover the scripted
    services, so the real `consult_staging` runs: its verdict, its alert and its ledger write
    are the ones under test, and `tick.staging_verdict` only says what exit codes the scripts
    hand back.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    scripted = ScriptedTick(repo, pathlib.Path(gitops_deploy.STAGING_OVERRIDE_FILE))
    monkeypatch.setattr(gitops_deploy, "REPO", str(repo))
    monkeypatch.setattr(deploy_io, "run", scripted.run)
    monkeypatch.setattr(gitops_deploy, "STAGING_GATE", True)
    monkeypatch.setattr(gitops_deploy, "STAGING_SUBSET", STAGING_SUBSET)
    scripted.tools = build_tools(scripted)
    return scripted
