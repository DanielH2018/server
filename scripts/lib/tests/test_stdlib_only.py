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

from lib import repo_paths, role_installs
from lib.repo_paths import REPO, SCRIPTS

FANOUT_REVIEW_UNIT = SCRIPTS / "dev" / "fanout_lib" / "review_unit.py"
SESSION_HEALTH = REPO / ".claude" / "hooks" / "session-health.py"
DEPLOY_UI = REPO / "hosts" / "components" / "deploy-ui" / "files" / "deploy_ui.py"
# One member per kind of no-venv caller: the three `--no-project` hooks, a module reached only
# through session-health's `REPO/scripts` insert, fan-out's dynamic review path, the land
# and fanout cc-wait sources, and the role scripts a host runs from its install directory.
MUST_INCLUDE = frozenset(
    {
        ".claude/hooks/session-health.py",
        ".claude/hooks/log-instructions.py",
        ".claude/hooks/fanout-stop.py",
        "scripts/lib/git.py",
        "scripts/dev/fanout.py",
        "scripts/dev/fanout_lib/review_unit.py",
        "scripts/deploy_tools/land_lib/land_probe.py",
        "hosts/components/deploy-ui/files/deploy_ui.py",
        "hosts/components/fake-remux/files/fake_remux.py",
        "hosts/components/fake-remux/files/mkv_attachment_repair.py",
        "hosts/components/fake-remux/files/state_push.py",
    }
)
# The `--project` hooks run in the venv, so they may import `yaml`.
MUST_EXCLUDE = frozenset(
    {
        ".claude/hooks/bash-pretool.py",
        ".claude/hooks/block-protected-bash.py",
        ".claude/hooks/block-protected-edits.py",
    }
)


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


def _role_dir(key) -> Path | None:
    """The role directory a map key names: the `Path` constant itself, or its name."""
    value = key if isinstance(key, Path) else getattr(repo_paths, str(key), None)
    if isinstance(value, Path) and value.resolve().is_relative_to(repo_paths.ROLES):
        return value.resolve()
    return None


def _role_map() -> dict[Path, list[str]]:
    """The `{constant: [module names]}` dict in repo_paths, found by shape, not by name.

    A key may be the `Path` constant or the constant's name; both normalise to the directory.
    """
    found = [
        value
        for name, value in vars(repo_paths).items()
        if name.isupper()
        and isinstance(value, dict)
        and value
        and all(_role_dir(k) is not None for k in value)
    ]
    if len(found) != 1:
        pytest.fail(
            f"repo_paths holds {len(found)} {{constant: [module names]}} maps, want exactly 1"
        )
    return {d: list(v) for k, v in found[0].items() if (d := _role_dir(k)) is not None}


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


def test_a_role_module_the_map_lists_is_accepted(tmp_path):
    checker = _checker()
    directory, names = next(iter(_role_map().items()))
    module = tmp_path / "importer.py"
    module.write_text(f"import {next(iter(names))}\n", encoding="utf-8")
    assert checker.walk(module, [tmp_path, directory]) == []


def test_stdlib_and_the_named_out_of_tree_modules_are_accepted(tmp_path):
    checker = _checker()
    module = tmp_path / "importer.py"
    module.write_text(
        "import json\nfrom pathlib import Path\nimport claude_guard\nimport claude_worktree\n"
    )
    assert checker.walk(module, [tmp_path]) == []


def test_the_role_map_names_role_modules_that_exist():
    for directory, names in _role_map().items():
        for name in names:
            assert (directory / f"{name}.py").is_file() or (
                directory / name
            ).is_dir(), (directory, name)


def test_every_member_of_the_set_passes_the_static_walk():
    # members(), not stdlib_only_set(): each staged copy against its own install's dirs only.
    checker = _checker()
    found = {
        str(path): bad
        for path, dirs in checker.members()
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
    for path, dirs in checker.members():
        ok, stderr = checker.runtime_import(path, dirs)
        if not ok:
            failed[str(path)] = stderr[-500:]
    assert failed == {}


def test_ansible_tests_is_a_repo_paths_constant():
    values = {
        v for k, v in vars(repo_paths).items() if k.isupper() and isinstance(v, Path)
    }
    assert repo_paths.ANSIBLE / "tests" in values


def _path_inserts(path: Path) -> list[ast.expr]:
    """The path argument of every `sys.path.insert(<index>, <path>)` call in `path`."""
    return [
        node.args[1]
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "insert"
        and len(node.args) == 2
    ]


@pytest.mark.parametrize(
    ("rel", "directory"),
    [
        ("scripts/lib/ansible_jinja_env.py", repo_paths.ANSIBLE / "filter_plugins"),
        ("scripts/dev/tighten_ratchets.py", repo_paths.ANSIBLE / "tests"),
    ],
    ids=["ansible_jinja_env-filter_plugins", "tighten_ratchets-ansible_tests"],
)
def test_the_two_hand_built_inserts_use_repo_paths_constants(rel, directory):
    inserts = _path_inserts(REPO / rel)
    hand_built = [
        ast.unparse(arg)
        for arg in inserts
        if "ansible" in ast.unparse(arg).lower()
        and any(
            isinstance(sub, ast.BinOp) and isinstance(sub.right, ast.Constant)
            for sub in ast.walk(arg)
        )
    ]
    assert hand_built == []
    named = {
        sub.id for arg in inserts for sub in ast.walk(arg) if isinstance(sub, ast.Name)
    }
    assert any(getattr(repo_paths, name, None) == directory for name in named), (
        f"no sys.path insert in {rel} names the repo_paths constant for {directory}"
    )


def test_the_declared_fanout_units_are_the_commands_the_builders_write():
    # FANOUT_UNITS copies two command lines built in Python; this keeps the copy honest.
    from fanout_lib.clean import remote_clean_command
    from fanout_lib.launch import review_command
    from fanout_lib.manifest import Batch
    from fanout_lib.notify import stop_post
    from fanout_lib.target import SERVER_TARGET
    from lib.stdlib_only import FANOUT_UNITS

    written = [
        review_command("1"),
        remote_clean_command(
            Batch("1", "daniel-box", "/w1", "wt-1", "fanout-1", [1], "t")
        ),
        stop_post("1", SERVER_TARGET),
    ]
    for entry, argv in FANOUT_UNITS:
        if entry.name == "fanout.py" and argv[:1] == ("place",):
            continue  # what the fanout_place.py shim forwards to, not a command line
        shape = " ".join([entry.relative_to(REPO).as_posix(), *argv]) + " "
        assert any(shape in command for command in written), shape


def _uv_gate_leg(drill: str) -> str:
    """Return the drill's `uv run --no-project` GATE_CMD array, without the `.venv` leg.

    A slice from the first `--no-project` would start at the comment above the `if`, so it
    would also hold the `.venv` leg, whose argv then satisfies an assertion meant for this one.
    """
    leg = drill[drill.index("/usr/bin/uv run --no-project") :]
    return leg[: leg.index("\n  )")]


def test_the_etcd_drill_gate_and_what_its_runbook_imports_are_in_the_set():
    from lib.stdlib_only import RUNBOOK_UNITS

    drill = (REPO / "scripts/backup/etcd_restore_drill.sh").read_text(encoding="utf-8")
    no_project = _uv_gate_leg(drill)
    for entry, argv in RUNBOOK_UNITS:
        assert f'GATE_SCRIPT="$REPO_ROOT/{entry.relative_to(REPO).as_posix()}"' in drill
        assert f'"$GATE_SCRIPT" {" ".join(argv)} ' in no_project
    members = _rel_set(_checker())
    assert {
        "scripts/deploy_tools/runbook_gates.py",
        "scripts/deploy_tools/runbook_gates_lib/etcd_restore.py",
    } <= set(members)
    # The lazy registry keeps the yaml-importing runbooks off the drill's path.
    assert "scripts/deploy_tools/runbook_gates_lib/longhorn_upgrade.py" not in members


def test_a_uv_leg_without_the_subcommand_fails_even_when_the_venv_leg_has_it():
    drill = (REPO / "scripts/backup/etcd_restore_drill.sh").read_text(encoding="utf-8")
    uv_leg = _uv_gate_leg(drill)
    assert '"$GATE_SCRIPT" etcd-restore ' in uv_leg
    broken = drill.replace(
        uv_leg, uv_leg.replace('"$GATE_SCRIPT" etcd-restore', '"$GATE_SCRIPT"')
    )
    assert '"$GATE_SCRIPT" etcd-restore ' in broken  # the .venv leg still carries it
    assert '"$GATE_SCRIPT" etcd-restore ' not in _uv_gate_leg(broken)


def _install(live_dir: str) -> Path:
    return role_installs.stage() / live_dir.lstrip("/")


def test_yaml_added_to_the_deploy_ui_daemon_turns_the_walk_red(tmp_path):
    checker = _checker()
    dirs = checker.stdlib_only_set()[DEPLOY_UI]
    assert _install("/usr/lib/homelab/deploy-ui") in dirs
    assert checker.walk(DEPLOY_UI, dirs) == []
    injected = tmp_path / "deploy_ui.py"
    injected.write_text(
        DEPLOY_UI.read_text(encoding="utf-8") + "\nimport yaml\n", encoding="utf-8"
    )
    assert "yaml" in checker.walk(injected, [tmp_path, *dirs])


def test_an_install_directory_resolves_only_what_its_role_ships(tmp_path):
    # configarr_status sits in the tree's role files, but renovate-notify's install does not
    # ship it.
    checker = _checker()
    module = tmp_path / "importer.py"
    module.write_text("import notify_logic\nimport host_lib\n", encoding="utf-8")
    assert checker.walk(module, [_install("/usr/lib/homelab/renovate-notify")]) == []
    unshipped = tmp_path / "unshipped.py"
    unshipped.write_text(
        "from configarr_lib import configarr_status\n", encoding="utf-8"
    )
    assert checker.walk(unshipped, [_install("/usr/lib/homelab/renovate-notify")]) == [
        "configarr_lib"
    ]


def test_a_staged_copy_is_checked_against_its_own_install_only():
    # host_lib.py ships to a dozen install directories; a union would resolve through siblings.
    checker = _checker()
    staged = _install("/usr/lib/homelab/renovate-notify") / "host_lib.py"
    dirs = dict(checker.members())[staged]
    assert dirs == [_install("/usr/lib/homelab/renovate-notify")]


DEPLOY_UI_UNIT = (
    REPO / "hosts" / "components" / "deploy-ui" / "templates" / "deploy-ui.service.j2"
)


def test_a_role_command_maps_to_its_installed_copy_is_clean():
    staged = role_installs.role_entry(
        "/usr/lib/homelab/deploy-ui/deploy_ui.py", DEPLOY_UI_UNIT
    )
    assert staged == _install("/usr/lib/homelab/deploy-ui") / "deploy_ui.py"


def test_a_role_command_no_copy_installs_is_flagged():
    with pytest.raises(ValueError, match="no copy task"):
        role_installs.role_entry(
            "/usr/lib/homelab/deploy-ui/never_shipped.py", DEPLOY_UI_UNIT
        )


def test_an_import_guarded_by_import_error_is_clean(tmp_path):
    checker = _checker()
    module = tmp_path / "importer.py"
    module.write_text(
        "try:\n    import yaml\nexcept (ImportError, OSError):\n    yaml = None\n",
        encoding="utf-8",
    )
    assert checker.walk(module, [tmp_path]) == []


def test_an_import_guarded_by_another_exception_is_flagged(tmp_path):
    checker = _checker()
    module = tmp_path / "importer.py"
    module.write_text(
        "try:\n    import yaml\nexcept ValueError:\n    yaml = None\n",
        encoding="utf-8",
    )
    assert checker.walk(module, [tmp_path]) == ["yaml"]


def test_runtime_import_passes_a_module_that_exits_after_its_imports(tmp_path):
    # longhorn_backup_health.py exits at import on an env var only its cron wrapper sets.
    module = tmp_path / "exits.py"
    module.write_text("import json\nraise SystemExit('LONGHORN_X is not set')\n")
    ok, stderr = _checker().runtime_import(module, [tmp_path])
    assert ok, stderr


EXITS_ON_IMPORT_ERROR = "try:\n    import yaml\nexcept ImportError:\n    raise SystemExit('install PyYAML')\n"


def test_an_import_whose_handler_exits_is_flagged(tmp_path):
    checker = _checker()
    module = tmp_path / "exits_on_import_error.py"
    module.write_text(EXITS_ON_IMPORT_ERROR, encoding="utf-8")
    assert checker.walk(module, [tmp_path]) == ["yaml"]


def test_runtime_import_fails_a_module_that_exits_on_a_failed_import(tmp_path):
    module = tmp_path / "exits_on_import_error.py"
    module.write_text(EXITS_ON_IMPORT_ERROR, encoding="utf-8")
    ok, stderr = _checker().runtime_import(module, [tmp_path])
    assert not ok
    assert "PyYAML" in stderr


def test_a_no_project_command_with_a_script_path_is_read_is_clean():
    text = "uv run --no-project --python 3.14 \\\n  /opt/x/run.py --dry-run\n"
    assert role_installs._scripts_run(text, DEPLOY_UI_UNIT) == ["/opt/x/run.py"]


@pytest.mark.parametrize(
    "text",
    [
        "uv run --no-project --python 3.14 -m pkg.mod\n",
        # A module run must not borrow the next command's script.
        "uv run --no-project -m pkg.mod\nuv run --frozen python /opt/x/run.py\n",
        "uv run --no-project " + "--flag " * 80 + "/opt/x/run.py\n",
    ],
    ids=["module-run", "next-commands-script", "past-the-scan"],
)
def test_a_no_project_command_with_no_readable_script_is_flagged(text):
    with pytest.raises(ValueError, match=r"runs no \.py path"):
        role_installs._scripts_run(text, DEPLOY_UI_UNIT)
