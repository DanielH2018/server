#!/usr/bin/env python3
"""Every runtime module in gitops_deploy's files/ must be in BOTH of the role's ship lists.

The deployer's Python lives in `roles/setup/gitops_deploy/files/`, and the role names each file
twice in `tasks/main.yml`: the `loop:` of the copy task that installs it under
`/opt/gitops-deploy/`, and the `stamp_deployed_pairs` that records its render provenance for
`manifest-prune-check.sh`. Neither list is derived from the directory, and until this test
nothing asserted that either covered `files/*.py`.

That gap has the same shape as the one `test_monitor_bridge_modules.py` closes for
monitor-bridge, and the same failure: pytest imports the modules from disk, so a module added
to files/ and forgotten in the copy loop passes CI and kills the deployer at import on its
next tick —

    ModuleNotFoundError: No module named 'deploy_classify'

— on the component that deploys everything else. `deploy_logic.py` is 1.4k lines and planned to
split, which is exactly when a new module appears here.

`host_lib.py` is installed by its own task from `roles/setup/common/files/` and is not in this
role's files/, so it is outside the set this test derives. Its stamp pair is asserted directly.

Run: uv run pytest ansible/tests/deploy/test_gitops_deploy_ship_list.py
"""

import ast

import yaml
from _helpers import REPO


ROLE = REPO / "ansible" / "roles" / "setup" / "gitops_deploy"
FILES = ROLE / "files"
ENTRYPOINT = "gitops_deploy.py"
INSTALL_DIR = "/opt/gitops-deploy"
COPY_TASK = "Install deployer Python files"
STAMP_TASK = "Record the deployed gitops-deploy code"


def _tasks():
    return yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())


def _task(name):
    matches = [t for t in _tasks() if t.get("name") == name]
    assert len(matches) == 1, (
        f"expected exactly one task named {name!r}, found {len(matches)}"
    )
    return matches[0]


def _copy_loop():
    """File names the copy task installs under /opt/gitops-deploy/."""
    return list(_task(COPY_TASK)["loop"])


def _stamp_pairs():
    return list(_task(STAMP_TASK)["vars"]["stamp_deployed_pairs"])


def _runtime_modules_in(files_dir):
    """The .py files that are production code, not the test suite."""
    return sorted(
        p.name
        for p in files_dir.glob("*.py")
        if not p.name.startswith("test_") and p.name != "conftest.py"
    )


def _missing_from_copy_loop(copy_loop, runtime_modules):
    return sorted(set(runtime_modules) - set(copy_loop))


def _missing_from_stamp_pairs(stamp_pairs, runtime_modules):
    stamped = {p["src"] for p in stamp_pairs}
    role_src = ROLE.relative_to(REPO)
    return sorted(m for m in runtime_modules if f"{role_src}/files/{m}" not in stamped)


# --- The copy loop -------------------------------------------------------------------------


def test_copy_loop_matches_the_runtime_modules_on_disk():
    assert sorted(_copy_loop()) == _runtime_modules_in(FILES)


def test_copy_loop_carries_the_entrypoint():
    """The unit runs /opt/gitops-deploy/gitops_deploy.py; the rest without it deploys nothing."""
    assert ENTRYPOINT in _copy_loop()


def test_copy_loop_excludes_the_test_suite():
    shipped = set(_copy_loop())
    tests = {p.name for p in FILES.glob("test_*.py")} | {"conftest.py"}
    assert not (shipped & tests)


def test_copy_loop_installs_into_the_directory_the_unit_runs_from():
    dest = _task(COPY_TASK)["ansible.builtin.copy"]["dest"]
    assert dest.startswith(INSTALL_DIR + "/"), dest


# --- The stamp pairs -----------------------------------------------------------------------


def test_every_runtime_module_has_a_stamp_pair():
    missing = _missing_from_stamp_pairs(_stamp_pairs(), _runtime_modules_in(FILES))
    assert not missing, f"no stamp_deployed_pairs entry for {missing}"


def test_stamp_pairs_point_at_the_installed_path():
    for pair in _stamp_pairs():
        assert pair["live"].startswith(INSTALL_DIR + "/"), pair
        assert (REPO / pair["src"]).is_file(), pair


def test_host_lib_is_stamped_from_the_common_role():
    """host_lib.py is installed by its own task from roles/setup/common; keep its pair."""
    srcs = {p["src"] for p in _stamp_pairs()}
    assert "ansible/roles/setup/common/files/host_lib.py" in srcs


# --- The import census ---------------------------------------------------------------------


def _imported_locally(entrypoint, local):
    tree = ast.parse((FILES / entrypoint).read_text())
    imported = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module in local
        ):
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in local:
                    imported.add(alias.name)
    return imported


def test_every_module_the_entrypoint_reaches_is_shipped():
    """The property that actually breaks the unit, asserted on its own.

    Set equality above already implies it; this keeps holding if the runtime/test split is
    ever loosened. It follows imports one level down, so it also covers what a facade
    `deploy_logic.py` re-exports after the split: every module the entrypoint reaches through
    the facade still has to be on disk.
    """
    shipped = {m[: -len(".py")] for m in _copy_loop()}
    local = {p.stem for p in FILES.glob("*.py")}
    imported = _imported_locally(ENTRYPOINT, local)
    for module in sorted(imported):
        imported |= _imported_locally(f"{module}.py", local)
    missing = imported - shipped
    assert not missing, (
        f"{ENTRYPOINT} reaches {sorted(missing)}, absent from the copy loop"
    )


# --- Proof the guard can go red ------------------------------------------------------------
#
# Each rule is exercised on an input it must reject as well as the live tree it accepts. A
# guard only ever observed passing has no evidence it can fail.


def test_current_shape_is_clean(tmp_path):
    (tmp_path / "gitops_deploy.py").write_text("")
    (tmp_path / "deploy_logic.py").write_text("")
    (tmp_path / "test_deploy_health.py").write_text("")
    runtime = _runtime_modules_in(tmp_path)
    assert (
        _missing_from_copy_loop(["gitops_deploy.py", "deploy_logic.py"], runtime) == []
    )
    assert _missing_from_stamp_pairs(_stamp_pairs(), runtime) == []


def test_module_missing_from_copy_loop_is_flagged(tmp_path):
    (tmp_path / "gitops_deploy.py").write_text("")
    (tmp_path / "deploy_logic.py").write_text("")
    (tmp_path / "deploy_classify.py").write_text("")
    runtime = _runtime_modules_in(tmp_path)
    assert _missing_from_copy_loop(
        ["gitops_deploy.py", "deploy_logic.py"], runtime
    ) == ["deploy_classify.py"]


def test_module_missing_from_stamp_pairs_is_flagged(tmp_path):
    (tmp_path / "gitops_deploy.py").write_text("")
    (tmp_path / "deploy_classify.py").write_text("")
    runtime = _runtime_modules_in(tmp_path)
    assert _missing_from_stamp_pairs(_stamp_pairs(), runtime) == ["deploy_classify.py"]
