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
3. A shell template's text. A guard that reads a `*.sh.j2`'s SOURCE matches `{{ ... }}` the
   moment a value it asserts on — a retry count, a URL, a threshold — moves into a role default,
   and a pattern that matches nothing passes (#3178). `ansible/tests/_shell_render.py` renders
   the whole roster through the same gate that shellchecks it, so an assertion cannot drift from
   what the host runs. Rule 3 catches the two spellings a deliberate reader takes: a path
   expression ending in a `*.sh.j2` name, and a `glob("*.sh.j2")` roster. It does NOT catch a
   census wider than that — `test_healthchecks_pings.py` reaches every template through an
   `rglob("*")`, which is #3190 rather than an entry in the list below.

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
        "ansible/tests/longhorn/_restore_drill.py",
        "ansible/tests/longhorn/test_longhorn_reap_guard.py",
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


# Test modules that still read a `*.sh.j2`'s source, each with the reason it is not converted.
# #3189 is the conversion; every entry carries the exposure rule 3 exists to stop, so this list
# only ever shrinks — `test_every_grandfathered_reader_still_reads_one` drops an entry that no
# longer reads one, and the rule then refuses it coming back. A module whose SUBJECT is the
# source text — a guard on the Jinja reference itself, which a render erases — stays here
# permanently with that written as its reason.
SHELL_SOURCE_READERS = {
    "ansible/roles/k8s/monitor-bridge/tests/test_check_swallowed_verdicts.py": "#3189",
    "ansible/tests/deploy/test_setup_render_manifest.py": "#3189",
    "ansible/tests/k8s/test_no_role_stages_files_in_a_pruned_manifest_dir.py": "#3189",
    "ansible/tests/setup/_pi_health.py": "#3189",
    "ansible/tests/setup/test_cron_scripts_publish_via_pr.py": "#3189",
    "ansible/tests/setup/test_docs_refresh_failure_path.py": "#3189",
    "ansible/tests/setup/test_docs_refresh_heartbeat.py": "#3189",
    "ansible/tests/setup/test_docs_refresh_records_shard_weights.py": "#3189",
    "ansible/tests/setup/test_eval_run_failure_path.py": "#3189",
    "ansible/tests/setup/test_eval_sweep_cron.py": "#3189",
    "ansible/tests/setup/test_github_interaction_limit.py": "#3189",
    "ansible/tests/setup/test_github_ruleset_drift.py": "#3189",
    "ansible/tests/setup/test_loki_route_witness.py": "#3189",
    "ansible/tests/setup/test_nut_host_secondary.py": "#3189",
    "ansible/tests/setup/test_pi_gz_integrity_sweep.py": "#3189",
    "ansible/tests/setup/test_release_staleness_push_grouped.py": "#3189",
    "ansible/tests/staging/_fence_probe.py": "#3189",
    "ansible/tests/staging/test_etcd_drill_vm.py": "#3189",
    "scripts/validate/tests/test_shell_template_cron_rules.py": "#3189",
}


# How a test gets at a shell template's text. `read_bytes` and `open` count: the rule is about
# reading the file, not about the decoding. `glob("*.sh.j2")` is the roster spelling of the same
# thing — a module that enumerates the templates itself is one that will read them next.
_READ_METHODS = frozenset({"read_text", "read_bytes", "open"})
_GLOB_METHODS = frozenset({"glob", "rglob"})


def _names_a_shell_template(node: ast.expr) -> bool:
    """Whether `node` holds a `*.sh.j2` FILENAME literal, as opposed to a glob pattern.

    `"*.sh.j2"` is a pattern rather than a name, and is handled by the glob clause below.
    """
    return any(
        isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and n.value.endswith(".sh.j2")
        and "*" not in n.value
        for n in ast.walk(node)
    )


def _shell_template_paths(tree: ast.Module) -> dict[str, str]:
    """The names `tree` binds to a path that ends in a `*.sh.j2` file.

    Only a path EXPRESSION counts — `ROLES / "setup" / "k3s" / "templates" / "x.sh.j2"`, or the
    same walk written as one `Path("...")` argument. A bare filename string is a name, not a
    path: `_shell_render`'s own (plane, role, name) triples are spelled that way, as is every
    roster that names the templates it expects to find.
    """
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        is_path = isinstance(value, ast.BinOp) and isinstance(value.op, ast.Div)
        is_path = is_path or (
            isinstance(value, ast.Call) and ast.unparse(value.func).endswith("Path")
        )
        # A template the module wrote under `tmp_path` is its own fixture rather than a deployed
        # script: the validator's tests build synthetic role trees, and reading one back is how
        # they check the renderer.
        if (
            is_path
            and _names_a_shell_template(value)
            and "tmp_path" not in ast.unparse(value)
        ):
            for target in targets:
                if isinstance(target, ast.Name):
                    bound[target.id] = ast.unparse(value)
    return bound


def shell_template_source_reads(source: str) -> list[str]:
    """The expressions in `source` that read a `*.sh.j2`'s SOURCE text, by how they spell it.

    A template written to `tmp_path` is the module's own fixture rather than a deployed script,
    so a receiver naming it is not a hit: the validator's tests build synthetic trees.
    """
    tree = ast.parse(source)
    bound = _shell_template_paths(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        receiver = ast.unparse(node.func.value)
        if "tmp_path" in receiver:
            continue
        reads_a_bound_name = node.func.attr in _READ_METHODS and (
            receiver in bound or _names_a_shell_template(node.func.value)
        )
        globs_the_roster = node.func.attr in _GLOB_METHODS and any(
            isinstance(a, ast.Constant)
            and isinstance(a.value, str)
            and a.value.endswith(".sh.j2")
            for a in node.args
        )
        if reads_a_bound_name or globs_the_roster:
            found.append(ast.unparse(node))
    return sorted(set(found))


def test_the_census_reaches_every_migrated_module():
    found = {_rel(p) for p in _test_modules()}
    missing = KNOWN_MEMBERS - found
    assert not missing, f"the scan no longer reaches: {sorted(missing)}"


def test_every_exemption_still_names_a_file_that_exists():
    """A stale exemption silently lets a whole module back out of the rule.

    Covers the grandfathered readers too: a deleted module left in that list makes
    `test_every_grandfathered_reader_still_reads_one` raise `FileNotFoundError` instead of
    naming the entry to drop.
    """
    listed = set(JINJA_EXEMPT) | set(SHELL_SOURCE_READERS)
    gone = sorted(name for name in listed if not (REPO / name).exists())
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


def test_every_grandfathered_reader_still_reads_one():
    """A converted module must leave the list, which is what keeps it shrinking.

    Without this, an entry outlives the read it was written for and silently re-permits the
    whole module — the same way a stale exemption does above.
    """
    clean = sorted(
        name
        for name in SHELL_SOURCE_READERS
        if (REPO / name).exists()
        and not shell_template_source_reads((REPO / name).read_text())
    )
    assert clean == [], (
        "these modules no longer read a shell template's source — drop them from "
        f"SHELL_SOURCE_READERS: {clean}"
    )


def test_no_new_test_module_reads_a_shell_templates_source():
    offenders = {
        _rel(p): reads
        for p in _test_modules()
        if _rel(p) not in SHELL_SOURCE_READERS
        and (reads := shell_template_source_reads(p.read_text()))
    }
    assert offenders == {}, (
        "these tests assert on a *.sh.j2's SOURCE, so a value moved into a role default leaves "
        "the assertion matching `{{ ... }}`. Use `_shell_render.rendered_shell_text(plane, "
        "role, name)`, or `render_shell_script` where the test runs the script: "
        f"{offenders}"
    )


def test_a_shell_template_source_read_is_flagged_in_each_spelling():
    for source in (
        'DRILL = K3S / "templates" / "drill.sh.j2"\nDRILL.read_text()\n',
        'T: Path = Path("ansible/roles/setup/k3s/templates/drill.sh.j2")\nT.read_bytes()\n',
        '(ROLES / "setup" / "k3s" / "templates" / "drill.sh.j2").read_text()\n',
        'for p in TEMPLATES.glob("*.sh.j2"):\n    pass\n',
    ):
        assert shell_template_source_reads(source) != [], source


def test_a_render_and_a_bare_name_are_not_flagged():
    """The sanctioned call takes a (plane, role, name) TRIPLE, so no path is built at all."""
    assert (
        shell_template_source_reads(
            "from _shell_render import rendered_shell_text\n"
            'SCRIPT = rendered_shell_text("setup", "k3s", "drill.sh.j2")\n'
        )
        == []
    )
    # A roster of names a census must find, and a synthetic template the module wrote itself.
    assert shell_template_source_reads('NAMES = frozenset({"drill.sh.j2"})\n') == []
    assert (
        shell_template_source_reads(
            'tpl = tmp_path / "good.sh.j2"\ntpl.write_text("")\ntpl.read_text()\n'
        )
        == []
    )
