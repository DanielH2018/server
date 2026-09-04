#!/usr/bin/env python3
"""Every Longhorn reap-orphan classifier module must be in BOTH ship lists in health-crons.yml.

Same shape and same failure as ansible/tests/deploy/test_gitops_deploy_ship_list.py: the role
names each file twice -- the `loop:` of the copy task that installs it under
`/opt/longhorn-reap/`, and the `stamp_deployed_pairs` that record its provenance for
`manifest-prune-check.sh`. Neither list is derived from the files/ directory, so a module added
there and forgotten in the copy loop passes CI and fails only when the shim actually runs:

    ModuleNotFoundError: No module named 'longhorn_reap_logic'

`ansible/roles/setup/k3s/files/` is the whole k3s role's shared files/ directory (also home to
live_drift_check.py, manifest_declares.py and non-Python manifests), not a directory dedicated
to this family the way gitops_deploy's is -- so the runtime-module census here is scoped by the
`longhorn_reap` filename prefix rather than "every .py in files/".

Run: uv run pytest ansible/roles/setup/k3s/tests/test_longhorn_reap_ship_list.py
"""

import ast

from lib import yaml_fast

from _helpers import REPO

ROLE = REPO / "ansible" / "roles" / "setup" / "k3s"
FILES = ROLE / "files"
ENTRYPOINTS = ["longhorn_reap_orphan_backups.py", "longhorn_reap_orphan_snapshots.py"]
INSTALL_DIR = "/opt/longhorn-reap"
COPY_TASK = "Install the Longhorn reap-orphan classifier scripts"
STAMP_TASK = "Record the deployed Longhorn reap-orphan classifiers"
PREFIX = "longhorn_reap"


def _tasks():
    return yaml_fast.safe_load((ROLE / "tasks" / "health-crons.yml").read_text())


def _task(name):
    matches = [t for t in _tasks() if t.get("name") == name]
    assert len(matches) == 1, (
        f"expected exactly one task named {name!r}, found {len(matches)}"
    )
    return matches[0]


HOST_LIB_INCLUDE = "{{ role_path }}/../common/tasks/install_host_lib.yml"


def _host_lib_include():
    """The shared-task include that installs host_lib.py into INSTALL_DIR, or None.

    host_lib.py is not in this role's copy loop: roles/setup/common/tasks/install_host_lib.yml
    installs it beside every consumer, and the caller names the directory. So the shipped
    set is the copy loop plus that one file, when the include is present and aimed here.
    """
    for t in _tasks():
        if t.get("ansible.builtin.import_tasks") == HOST_LIB_INCLUDE and (
            t.get("vars", {}).get("host_lib_dir") == INSTALL_DIR
        ):
            return t
    return None


def _copy_loop_basenames():
    """The host filename each file that lands in INSTALL_DIR: every copy-loop entry's
    basename, plus host_lib.py when the shared include installs it there."""
    names = [item.rsplit("/", 1)[-1] for item in _task(COPY_TASK)["loop"]]
    if _host_lib_include() is not None:
        names.append("host_lib.py")
    return sorted(names)


def _stamp_pairs():
    return list(_task(STAMP_TASK)["vars"]["stamp_deployed_pairs"])


def _runtime_modules_in(files_dir, prefix):
    """The reap-orphan family's own .py files, not the test suite and not a sibling family's."""
    return sorted(
        p.name
        for p in files_dir.glob(f"{prefix}*.py")
        if not p.name.startswith("test_")
    )


def _missing_from_copy_loop(copy_basenames, runtime_modules):
    return sorted(set(runtime_modules) - set(copy_basenames))


def _missing_from_stamp_pairs(stamp_pairs, runtime_modules, src_prefix):
    stamped = {p["src"] for p in stamp_pairs}
    return sorted(m for m in runtime_modules if f"{src_prefix}/{m}" not in stamped)


# --- The copy loop -------------------------------------------------------------------------


def test_copy_loop_matches_the_runtime_modules_on_disk_plus_host_lib():
    local_modules = _runtime_modules_in(FILES, PREFIX)
    assert _copy_loop_basenames() == sorted(local_modules + ["host_lib.py"])


def test_copy_loop_carries_both_entrypoints():
    loop = _copy_loop_basenames()
    for entry in ENTRYPOINTS:
        assert entry in loop


def test_copy_loop_installs_into_the_directory_the_shims_run_from():
    dest = _task(COPY_TASK)["ansible.builtin.copy"]["dest"]
    assert dest.startswith(INSTALL_DIR + "/"), dest


def test_host_lib_is_copied_from_the_common_role_not_duplicated_in_k3s():
    """The sibling copy is what `import host_lib` resolves at runtime -- see host_lib.py's own
    docstring. A second, edited copy under roles/setup/k3s/files/ is how two callers quietly
    stop agreeing about what a kubectl failure looks like."""
    loop = _task(COPY_TASK)["loop"]
    assert not [item for item in loop if item.rsplit("/", 1)[-1] == "host_lib.py"], (
        "host_lib.py is installed by the shared include, not by this role's copy loop"
    )
    assert _host_lib_include() is not None, (
        f"no import_tasks of {HOST_LIB_INCLUDE} with host_lib_dir={INSTALL_DIR}"
    )
    assert not (FILES / "host_lib.py").exists()


# --- The stamp pairs -----------------------------------------------------------------------


def test_every_runtime_module_has_a_stamp_pair():
    local_modules = _runtime_modules_in(FILES, PREFIX)
    missing = _missing_from_stamp_pairs(
        _stamp_pairs(), local_modules, "ansible/roles/setup/k3s/files"
    )
    assert not missing, f"no stamp_deployed_pairs entry for {missing}"


def test_host_lib_is_stamped_from_the_common_role():
    srcs = {p["src"] for p in _stamp_pairs()}
    assert "ansible/roles/setup/common/files/host_lib.py" in srcs


def test_stamp_pairs_point_at_the_installed_path_and_a_real_file():
    for pair in _stamp_pairs():
        assert pair["live"].startswith(INSTALL_DIR + "/"), pair
        assert (REPO / pair["src"]).is_file(), pair


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


def test_every_module_each_entrypoint_imports_is_shipped():
    """The property that actually breaks the shim, asserted on its own: `host_lib` and
    `longhorn_reap_logic` are not local .py files in FILES/ for host_lib's case (it lives in
    common/), so this checks against the copy loop's basenames rather than a directory glob."""
    shipped = {name[: -len(".py")] for name in _copy_loop_basenames()}
    local = {"host_lib", "longhorn_reap_logic"}
    for entrypoint in ENTRYPOINTS:
        imported = _imported_locally(entrypoint, local)
        missing = imported - shipped
        assert not missing, (
            f"{entrypoint} reaches {sorted(missing)}, absent from the copy loop"
        )


# --- Proof the guard can go red -------------------------------------------------------------
#
# Each rule is exercised on an input it must reject as well as the live tree it accepts. A
# guard only ever observed passing has no evidence it can fail.


def test_current_shape_is_clean(tmp_path):
    (tmp_path / "longhorn_reap_logic.py").write_text("")
    (tmp_path / "longhorn_reap_orphan_backups.py").write_text("")
    (tmp_path / "test_longhorn_reap_logic.py").write_text("")
    runtime = _runtime_modules_in(tmp_path, PREFIX)
    copy_basenames = ["longhorn_reap_logic.py", "longhorn_reap_orphan_backups.py"]
    assert _missing_from_copy_loop(copy_basenames, runtime) == []


def test_module_missing_from_copy_loop_is_flagged(tmp_path):
    (tmp_path / "longhorn_reap_logic.py").write_text("")
    (tmp_path / "longhorn_reap_orphan_backups.py").write_text("")
    (tmp_path / "longhorn_reap_orphan_snapshots.py").write_text("")
    runtime = _runtime_modules_in(tmp_path, PREFIX)
    copy_basenames = ["longhorn_reap_logic.py", "longhorn_reap_orphan_backups.py"]
    assert _missing_from_copy_loop(copy_basenames, runtime) == [
        "longhorn_reap_orphan_snapshots.py"
    ]


def test_module_missing_from_stamp_pairs_is_flagged(tmp_path):
    (tmp_path / "longhorn_reap_logic.py").write_text("")
    (tmp_path / "longhorn_reap_orphan_backups.py").write_text("")
    runtime = _runtime_modules_in(tmp_path, PREFIX)
    stamp_pairs = [{"src": "ansible/roles/setup/k3s/files/longhorn_reap_logic.py"}]
    assert _missing_from_stamp_pairs(
        stamp_pairs, runtime, "ansible/roles/setup/k3s/files"
    ) == ["longhorn_reap_orphan_backups.py"]
