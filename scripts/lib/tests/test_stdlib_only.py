"""The modules a no-venv caller reaches import with the stdlib alone, checked statically and at runtime.

A hook `run-hook.sh` runs `--no-project`, a cc-wait source and fan-out's review unit all run
outside the project venv, so a third-party import anywhere they reach dies at import behind a
green suite whose venv has the package. #4167 was one: fan-out review died on `yaml`. Each such
break got its own one-path test after the fact (#4404).

The contract these tests pin, in `scripts/lib/stdlib_only.py`:

- `stdlib_only_set() -> dict[Path, list[Path]]` maps each module a no-venv caller reaches
  (absolute, resolved) to the directories that caller puts on `sys.path`, derived from the
  caller's own inserts rather than assumed to be `scripts/`.
- `walk(path, dirs) -> list[str]` returns the imports of `path` that are none of: the stdlib, a
  first-party module under `dirs`, a role module the `repo_paths` map lists for a constant in
  `dirs`, or the out-of-tree `claude_guard`/`claude_worktree`. An import of a basename two role
  directories share is accepted only through the map.
- `runtime_import(path, dirs) -> tuple[bool, str]` imports `path` in a fresh interpreter with no
  site-packages and `PYTHONPATH` set to `dirs`, returning whether it imported and its stderr.

Run: uv run pytest scripts/lib/tests/test_stdlib_only.py
"""

import ast
import importlib
from pathlib import Path

import pytest

from lib import repo_paths
from lib.repo_paths import REPO, SCRIPTS

FANOUT_REVIEW_UNIT = SCRIPTS / "dev" / "fanout_lib" / "review_unit.py"
SESSION_HEALTH = REPO / ".claude" / "hooks" / "session-health.py"
# One member per kind of no-venv caller: the three `--no-project` hooks, a module reached only
# through session-health's `REPO/scripts` insert, fan-out's dynamic review path, and the land
# and fanout cc-wait sources.
MUST_INCLUDE = frozenset(
    {
        ".claude/hooks/session-health.py",
        ".claude/hooks/log-instructions.py",
        ".claude/hooks/fanout-stop.py",
        "scripts/lib/git.py",
        "scripts/dev/fanout.py",
        "scripts/dev/fanout_lib/review_unit.py",
        "scripts/deploy_tools/land_lib/land_probe.py",
    }
)
# The `--project` hooks run in the venv; secret_bearing_host_paths.py imports `yaml` legitimately.
MUST_EXCLUDE = frozenset(
    {
        ".claude/hooks/bash-pretool.py",
        ".claude/hooks/block-protected-bash.py",
        ".claude/hooks/block-protected-edits.py",
        "scripts/secrets_mgmt/secret_bearing_host_paths.py",
    }
)
GITOPS_COLLIDING = ("gitops_hold", "gitops_markers", "gitops_ledger")


def _checker():
    try:
        return importlib.import_module("lib.stdlib_only")
    except ModuleNotFoundError as exc:
        pytest.fail(f"scripts/lib/stdlib_only.py does not exist yet: {exc}")


def _rel_set(checker) -> dict[str, list[Path]]:
    return {
        path.relative_to(REPO).as_posix(): dirs
        for path, dirs in checker.stdlib_only_set().items()
    }


def _role_map() -> dict[str, list[str]]:
    """The `{constant name: [module names]}` dict in repo_paths, found by shape, not by name."""
    found = [
        value
        for name, value in vars(repo_paths).items()
        if name.isupper()
        and isinstance(value, dict)
        and value
        and all(
            isinstance(k, str) and isinstance(getattr(repo_paths, k, None), Path)
            for k in value
        )
    ]
    if len(found) != 1:
        pytest.fail(
            f"repo_paths holds {len(found)} {{constant: [module names]}} maps, want exactly 1"
        )
    return found[0]


def test_the_set_holds_every_kind_of_no_venv_caller():
    members = _rel_set(_checker())
    assert MUST_INCLUDE - set(members) == set()


def test_the_set_leaves_out_the_project_hooks_and_what_only_they_reach():
    members = _rel_set(_checker())
    assert MUST_EXCLUDE & set(members) == set()


def test_session_health_dirs_are_its_own_inserts_not_just_scripts():
    dirs = {
        Path(d).resolve()
        for d in _rel_set(_checker())[".claude/hooks/session-health.py"]
    }
    assert {SESSION_HEALTH.parent, SCRIPTS} <= dirs


def test_a_module_reached_only_through_a_callers_insert_gets_that_callers_dirs():
    # scripts/lib/git.py has no sys.path insert of its own; session-health's REPO/scripts reaches it.
    dirs = {Path(d).resolve() for d in _rel_set(_checker())["scripts/lib/git.py"]}
    assert SCRIPTS in dirs


def test_yaml_added_to_the_fanout_review_path_turns_the_walk_red(tmp_path):
    checker = _checker()
    dirs = checker.stdlib_only_set()[FANOUT_REVIEW_UNIT]
    assert checker.walk(FANOUT_REVIEW_UNIT, dirs) == []
    injected = tmp_path / "review_unit.py"
    injected.write_text(
        FANOUT_REVIEW_UNIT.read_text(encoding="utf-8") + "\nimport yaml\n",
        encoding="utf-8",
    )
    assert "yaml" in checker.walk(injected, [tmp_path, *dirs])


def test_a_colliding_gitops_basename_outside_the_map_turns_the_walk_red(tmp_path):
    checker = _checker()
    role_map = _role_map()
    unmapped = [
        (const, name)
        for const in ("GITOPS_DEPLOY_FILES", "MONITOR_BRIDGE_FILES")
        for name in GITOPS_COLLIDING
        if name not in role_map.get(const, [])
    ]
    assert unmapped, (
        "the map sanctions every gitops basename under both roles; nothing is decidable"
    )
    const, name = unmapped[0]
    module = tmp_path / "importer.py"
    module.write_text(f"import {name}\n", encoding="utf-8")
    assert name in checker.walk(module, [tmp_path, getattr(repo_paths, const)])


def test_a_role_module_the_map_lists_is_accepted(tmp_path):
    checker = _checker()
    const, names = next(iter(_role_map().items()))
    module = tmp_path / "importer.py"
    module.write_text(f"import {names[0]}\n", encoding="utf-8")
    assert checker.walk(module, [tmp_path, getattr(repo_paths, const)]) == []


def test_stdlib_and_the_named_out_of_tree_modules_are_accepted(tmp_path):
    checker = _checker()
    module = tmp_path / "importer.py"
    module.write_text(
        "import json\nfrom pathlib import Path\nimport claude_guard\nimport claude_worktree\n"
    )
    assert checker.walk(module, [tmp_path]) == []


def test_the_role_map_names_role_directories_and_modules_that_exist():
    role_map = _role_map()
    for const, names in role_map.items():
        directory = getattr(repo_paths, const)
        assert directory.is_relative_to(repo_paths.ROLES), const
        for name in names:
            assert (directory / f"{name}.py").is_file() or (
                directory / name
            ).is_dir(), (const, name)


def test_every_member_of_the_set_passes_the_static_walk():
    checker = _checker()
    found = {
        path.relative_to(REPO).as_posix(): bad
        for path, dirs in checker.stdlib_only_set().items()
        if (bad := checker.walk(path, dirs))
    }
    assert found == {}


def test_runtime_import_fails_a_module_that_needs_a_site_package(tmp_path):
    checker = _checker()
    module = tmp_path / "needs_yaml.py"
    module.write_text("import yaml\n", encoding="utf-8")
    ok, stderr = checker.runtime_import(module, [tmp_path])
    assert not ok
    assert "yaml" in stderr


def test_runtime_import_resolves_a_module_through_its_callers_dirs():
    ok, stderr = _checker().runtime_import(SCRIPTS / "lib" / "git.py", [SCRIPTS])
    assert ok, stderr


def test_every_member_of_the_set_imports_outside_the_venv():
    checker = _checker()
    failed = {}
    for path, dirs in checker.stdlib_only_set().items():
        ok, stderr = checker.runtime_import(path, dirs)
        if not ok:
            failed[path.relative_to(REPO).as_posix()] = stderr[-500:]
    assert failed == {}


def test_ansible_tests_is_a_repo_paths_constant():
    values = {
        v for k, v in vars(repo_paths).items() if k.isupper() and isinstance(v, Path)
    }
    assert repo_paths.ANSIBLE / "tests" in values


def _insert_args_built_from_ansible_literals(path: Path) -> list[str]:
    """Each `sys.path.insert` argument in `path` that joins an `ansible` path to a string literal.

    Such an argument builds the directory by hand where a `repo_paths` constant should name it.
    """
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "insert" or len(node.args) != 2:
            continue
        arg = ast.unparse(node.args[1])
        if any(
            isinstance(sub, ast.BinOp)
            and isinstance(sub.op, ast.Div)
            and isinstance(sub.right, ast.Constant)
            for sub in ast.walk(node.args[1])
        ) and ("ansible" in arg.lower()):
            found.append(arg)
    return found


@pytest.mark.parametrize(
    "rel",
    ["scripts/lib/ansible_jinja_env.py", "scripts/dev/tighten_ratchets.py"],
    ids=["ansible_jinja_env-filter_plugins", "tighten_ratchets-ansible_tests"],
)
def test_the_two_hand_built_inserts_use_repo_paths_constants(rel):
    assert _insert_args_built_from_ansible_literals(REPO / rel) == []
