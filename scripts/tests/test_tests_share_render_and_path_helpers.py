"""A test renders Jinja and finds the repo root through the shared helpers, never by hand.

Two things a test module does not re-derive for itself:

1. A Jinja environment. A bare `jinja2.Environment()` has none of Ansible's whitespace flags
   and none of its filters, so a template renders under it differently from a deploy: a
   `{% for %}` leaves a blank line per iteration, and `| bool` fails or means Python's
   `bool()`. Twenty-seven tests built one, each choosing its own subset of the flags. The
   sanctioned paths are `lib.ansible_jinja_env.make_ansible_env` / `template_env` for a
   template's text, and `ansible/tests/_helpers.py`'s `jinja_env` / `render_expr` for a task
   expression that yields a structure.
2. The repo root. Forty-seven test modules computed it from `Path(__file__)` under three
   names, where `lib.repo_paths.REPO` already is that path.

This is the sibling of `test_scratch_repos_go_through_git_testing.py`, which holds the same
line for scratch git repositories.

Both rules are read from the AST, so a docstring naming the old form is prose rather than a
hit. The repo-root rule evaluates the path arithmetic against the module's own location, so it
flags `parents[3]` in one directory and passes the same text where it lands elsewhere.

Run: uv run pytest scripts/tests/test_tests_share_render_and_path_helpers.py
"""

import ast
from pathlib import Path

from lib.repo_paths import ANSIBLE, REPO, SCRIPTS
from test_script_bootstraps_present import is_pytest_only

# Every `testpaths` entry that holds Python, so the rule covers what pytest collects.
_TEST_ROOTS = (
    SCRIPTS,
    ANSIBLE / "tests",
    ANSIBLE / "roles",
    REPO / ".claude" / "hooks",
    REPO / ".claude" / "tests",
    REPO / "evals",
)

# Modules allowed a bare Jinja environment, each with the reason it is not an Ansible render.
JINJA_EXEMPT = {
    # The sanctioned expression helper itself: `jinja_env` is the NativeEnvironment every
    # other test borrows.
    "ansible/tests/_helpers.py",
    # Renders Home Assistant's Jinja, not Ansible's. Ansible's filters would be wrong there.
    "ansible/roles/k8s/home-assistant/tests/jinja_harness.py",
}

# Modules the census must reach. An empty or partial scan means the walk stopped matching
# rather than that the tree is clean.
KNOWN_MEMBERS = frozenset(
    {
        ".claude/hooks/tests/test_hook_scripts_executable.py",
        "ansible/roles/k8s/monitor-bridge/tests/test_check_ha.py",
        "ansible/tests/k8s/_manifest_guards.py",
        "ansible/tests/setup/test_kuma_check_timer.py",
        "ansible/tests/staging/test_staging_network.py",
        "scripts/deploy_tools/tests/test_deploy_exit_codes.py",
        "scripts/docs/tests/test_mkdocs_repo_links.py",
    }
)

_ENV_CLASSES = frozenset({"Environment", "NativeEnvironment"})
_JINJA_MODULES = frozenset({"jinja2", "jinja2.nativetypes"})


def _test_modules() -> list[Path]:
    found = []
    for root in _TEST_ROOTS:
        for path in root.rglob("*.py"):
            rel = path.relative_to(REPO)
            if "collections" not in rel.parts and is_pytest_only(rel):
                found.append(path)
    return sorted(set(found))


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO))


def bare_jinja_envs(source: str) -> list[str]:
    """The Jinja environment classes `source` constructs directly, by the name it calls.

    A name counts only when it was imported from `jinja2` or `jinja2.nativetypes`, so
    `liquid.Environment()` — a different template language — is not a hit.
    """
    tree = ast.parse(source)
    bound: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _JINJA_MODULES:
            bound.update(
                a.asname or a.name for a in node.names if a.name in _ENV_CLASSES
            )
        elif isinstance(node, ast.Import):
            modules.update(
                a.asname or a.name for a in node.names if a.name in _JINJA_MODULES
            )
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in bound:
            found.append(func.id)
        elif (
            isinstance(func, ast.Attribute)
            and func.attr in _ENV_CLASSES
            and ast.unparse(func.value) in modules
        ):
            found.append(ast.unparse(func))
    return found


def _eval_file_path(node: ast.expr, module: Path) -> Path | None:
    """The path `node` computes from `__file__` in `module`, or None if it is not that shape.

    Understands `Path(__file__)`, `.resolve()`, `.absolute()`, `.parent`, `.parents[n]`, and
    the `os.path.dirname`/`abspath`/`realpath` spelling of the same walk.
    """
    if isinstance(node, ast.Name) and node.id == "__file__":
        return module
    if isinstance(node, ast.Call):
        name = ast.unparse(node.func)
        if name in {"Path", "pathlib.Path"} and len(node.args) == 1:
            return _eval_file_path(node.args[0], module)
        if name in {"os.path.abspath", "os.path.realpath"} and len(node.args) == 1:
            return _eval_file_path(node.args[0], module)
        if name == "os.path.dirname" and len(node.args) == 1:
            inner = _eval_file_path(node.args[0], module)
            return None if inner is None else inner.parent
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {"resolve", "absolute"}
            and not node.args
        ):
            return _eval_file_path(node.func.value, module)
        return None
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        inner = _eval_file_path(node.value, module)
        return None if inner is None else inner.parent
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "parents"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, int)
    ):
        inner = _eval_file_path(node.value.value, module)
        if inner is None or node.slice.value >= len(inner.parents):
            return None
        return inner.parents[node.slice.value]
    return None


def local_repo_roots(source: str, module: Path, repo: Path = REPO) -> list[str]:
    """The names `source` binds to `repo` by walking up from `__file__` at `module`."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if _eval_file_path(value, module) == repo:
            found.extend(ast.unparse(t) for t in targets)
    return found


def test_the_census_reaches_every_migrated_module():
    found = {_rel(p) for p in _test_modules()}
    missing = KNOWN_MEMBERS - found
    assert not missing, f"the scan no longer reaches: {sorted(missing)}"


def test_every_exemption_still_names_a_file_that_exists():
    """A stale exemption silently lets a whole module back out of the rule."""
    gone = sorted(name for name in JINJA_EXEMPT if not (REPO / name).exists())
    assert gone == [], f"exempted modules that no longer exist: {gone}"


def test_no_test_module_builds_a_bare_jinja_environment():
    offenders = {
        _rel(p): envs
        for p in _test_modules()
        if _rel(p) not in JINJA_EXEMPT and (envs := bare_jinja_envs(p.read_text()))
    }
    assert offenders == {}, (
        "these tests build their own Jinja environment, which renders a template without "
        "Ansible's whitespace flags and filters. Use `lib.ansible_jinja_env.make_ansible_env` "
        "(pass `undefined_cls=` to keep a strict render) or `_helpers.jinja_env` for a "
        f"structure-valued expression: {offenders}"
    )


def test_no_test_module_computes_the_repo_root():
    offenders = {
        _rel(p): names
        for p in _test_modules()
        if (names := local_repo_roots(p.read_text(), p.resolve()))
    }
    assert offenders == {}, (
        "these tests derive the repo root from `__file__`; import `REPO` from "
        f"`lib.repo_paths` instead (`REPO as _REPO` keeps a local name): {offenders}"
    )


def test_the_shared_helpers_are_clean():
    assert (
        bare_jinja_envs(
            "from lib.ansible_jinja_env import make_ansible_env\nmake_ansible_env()\n"
        )
        == []
    )
    assert local_repo_roots("from lib.repo_paths import REPO\n", REPO / "t.py") == []


def test_a_bare_jinja_environment_is_flagged_in_each_spelling():
    assert bare_jinja_envs("from jinja2 import Environment\nEnvironment()\n") == [
        "Environment"
    ]
    assert bare_jinja_envs("import jinja2\njinja2.Environment(trim_blocks=True)\n") == [
        "jinja2.Environment"
    ]
    assert bare_jinja_envs(
        "from jinja2.nativetypes import NativeEnvironment as N\nN()\n"
    ) == ["N"]


def test_another_template_language_is_not_flagged():
    """`liquid.Environment` renders Kuma's notification bodies, not an Ansible template."""
    assert bare_jinja_envs("import liquid\nliquid.Environment()\n") == []
    assert bare_jinja_envs('"""jinja2.Environment() used to be built here."""\n') == []


def test_a_local_repo_root_is_flagged_in_each_spelling(tmp_path):
    module = tmp_path / "scripts" / "x" / "tests" / "test_y.py"
    for source in (
        "REPO = Path(__file__).resolve().parents[3]\n",
        "_REPO: Path = Path(__file__).parent.parent.parent.parent\n",
        "ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname("
        "os.path.abspath(__file__)))))\n",
    ):
        assert local_repo_roots(source, module, repo=tmp_path) != [], source


def test_a_path_short_of_the_repo_root_is_not_flagged(tmp_path):
    """`parents[1]` is the module's own role or package, which is not what REPO names."""
    module = tmp_path / "scripts" / "x" / "tests" / "test_y.py"
    assert (
        local_repo_roots(
            "HERE = Path(__file__).resolve().parents[1]\n", module, repo=tmp_path
        )
        == []
    )
