#!/usr/bin/env python3
"""Every runtime module in gitops_deploy's files/ must be in BOTH of the role's ship lists.

The deployer's Python lives in `roles/setup/gitops_deploy/files/`: the entry point and the
`gitops_*` modules flat, and the `deploy_*` libraries in the `deployer_lib/` namespace package
(#4355). The role names each file twice in `tasks/code.yml`: the `loop:` of the copy task that
installs it under `/opt/gitops-deploy/`, and the `stamp_deployed_pairs` that records its render
provenance for `manifest-prune-check.sh`. Neither list is derived from the directory, and this
test asserts that each covers `files/*.py` and `files/deployer_lib/*.py`.

That gap has the same shape as the one `test_monitor_bridge_modules.py` closes for
monitor-bridge, and the same failure: pytest imports the modules from disk, so a module added
to files/ and forgotten in the copy loop passes CI and kills the deployer at import on its
next tick —

    ModuleNotFoundError: No module named 'deploy_classify'

— on the component that deploys everything else. A split is exactly when a new module appears
here.

The copy task's `dest:` is rendered per item rather than read off the loop. A `dest` that
flattens the package puts the libraries exactly where the removal task deletes them, and the
deployer then fails at import on every tick. The last tests install the shipped files into a
scratch directory the way the role does, and import the entry point from there.

`host_lib.py` is installed by its own task from `hosts/lib/` and is not in this
role's files/, so it is outside the set this test derives. Its stamp pair is asserted directly.

Run: uv run pytest ansible/tests/deploy/test_gitops_deploy_ship_list.py
"""

import ast
import posixpath
import shutil
import subprocess
import sys

from _helpers import REPO, load_tasks
from lib.ansible_jinja_env import make_ansible_env
from lib.repo_paths import HOST_LIB_FILES


ROLE = REPO / "ansible" / "roles" / "setup" / "gitops_deploy"
INSTALL = ROLE / "tasks" / "code.yml"
FILES = ROLE / "files"
PACKAGE = "deployer_lib"
TESTS = ROLE / "tests"
ENTRYPOINT = "gitops_deploy.py"
INSTALL_DIR = "/opt/gitops-deploy"
COPY_TASK = "Install deployer Python files"
# The module `deploy_git` imports from hosts/lib/, which the role's own task installs.
HOST_LIB_ONLY = ("ci_verdict.py",)
REMOVE_TASK = "Remove the deployer libraries that moved into deployer_lib"
STAMP_TASK = "Record the deployed gitops-deploy code"
CI_GATE_TASK = "Install the CI gate module beside the deployer"


def _task(name):
    matches = [t for t in load_tasks(INSTALL) if t.get("name") == name]
    assert len(matches) == 1, (
        f"expected exactly one task named {name!r}, found {len(matches)}"
    )
    return matches[0]


def _copy_loop():
    """Paths under files/ the copy task installs, `deployer_lib/` ones included."""
    return list(_task(COPY_TASK)["loop"])


def _stamp_pairs():
    return list(_task(STAMP_TASK)["vars"]["stamp_deployed_pairs"])


def _runtime_modules_in(files_dir):
    """The production .py files in `files_dir` and its package, relative to `files_dir`."""
    found = list(files_dir.glob("*.py")) + list((files_dir / PACKAGE).glob("*.py"))
    return sorted(
        str(p.relative_to(files_dir))
        for p in found
        if not p.name.startswith("test_") and p.name != "conftest.py"
    )


def _missing_from_copy_loop(copy_loop, runtime_modules):
    return sorted(set(runtime_modules) - set(copy_loop))


def _missing_from_stamp_pairs(stamp_pairs, runtime_modules):
    stamped = {p["src"] for p in stamp_pairs}
    role_src = ROLE.relative_to(REPO)
    return sorted(m for m in runtime_modules if f"{role_src}/files/{m}" not in stamped)


def _installed(dest_template=None):
    """Each shipped file mapped to the path the copy task's `dest:` renders for it."""
    env = make_ansible_env()
    # Ansible's own path filter, which the shared registry does not carry.
    env.filters.setdefault("basename", posixpath.basename)
    dest = env.from_string(
        dest_template or _task(COPY_TASK)["ansible.builtin.copy"]["dest"]
    )
    return {rel: dest.render(item=rel) for rel in _copy_loop()}


# --- The copy loop -------------------------------------------------------------------------


def test_copy_loop_matches_the_runtime_modules_on_disk():
    runtime = _runtime_modules_in(FILES)
    # Named members, so a move that empties either glob fails here rather than on nothing.
    assert {ENTRYPOINT, "gitops_markers.py", f"{PACKAGE}/deploy_logic.py"} <= set(
        runtime
    )
    assert sorted(_copy_loop()) == runtime


def test_copy_loop_carries_the_entrypoint_after_its_libraries():
    """The unit runs /opt/gitops-deploy/gitops_deploy.py, which imports everything else."""
    assert _copy_loop()[-1] == ENTRYPOINT, "the entry point must land after its imports"


def test_copy_loop_excludes_the_test_suite():
    shipped = {rel.rsplit("/", 1)[-1] for rel in _copy_loop()}
    tests = {p.name for p in TESTS.glob("*.py")}
    assert tests, "the suite moved again — point TESTS at it"
    assert not (shipped & tests)


def test_copy_loop_keeps_the_package_directory_on_the_host():
    installed = _installed()
    assert installed[ENTRYPOINT] == f"{INSTALL_DIR}/{ENTRYPOINT}"
    assert (
        installed[f"{PACKAGE}/deploy_logic.py"]
        == f"{INSTALL_DIR}/{PACKAGE}/deploy_logic.py"
    )


def test_the_removed_flat_files_are_ones_the_role_no_longer_ships():
    removed = _task(REMOVE_TASK)["loop"]
    assert "deploy_logic.py" in removed
    for name in removed:
        assert not (FILES / name).exists(), (
            f"{name} still ships flat, so removing it breaks the deployer"
        )
        assert (FILES / PACKAGE / name).is_file(), (
            f"{name} is not in {PACKAGE}/, so nothing replaced it"
        )


# --- The stamp pairs -----------------------------------------------------------------------


def test_every_runtime_module_has_a_stamp_pair():
    missing = _missing_from_stamp_pairs(_stamp_pairs(), _runtime_modules_in(FILES))
    assert not missing, f"no stamp_deployed_pairs entry for {missing}"


def test_every_shipped_file_is_stamped_at_its_installed_path():
    pairs = {(p["live"], p["src"]) for p in _stamp_pairs()}
    role_src = ROLE.relative_to(REPO)
    for rel, live in _installed().items():
        assert (live, f"{role_src}/files/{rel}") in pairs, rel


def test_stamp_pairs_point_at_the_installed_path():
    for pair in _stamp_pairs():
        assert pair["live"].startswith(INSTALL_DIR + "/"), pair
        assert (REPO / pair["src"]).is_file(), pair


def test_host_lib_is_stamped_from_hosts_lib():
    """host_lib.py is installed by its own task from hosts/lib/; keep its pair."""
    srcs = {p["src"] for p in _stamp_pairs()}
    assert "hosts/lib/host_lib.py" in srcs


def test_the_ci_gate_module_lands_before_the_copy_loop_and_is_stamped():
    """`deploy_git` imports `ci_verdict` by bare name, so it must be on the host first.

    A `deploy_git.py` that lands before `ci_verdict.py` dies at import on the next tick. The
    module sits in `hosts/lib/`, outside this role's files/, so it has its own task and pair.
    """
    names = [t.get("name") for t in load_tasks(INSTALL)]
    assert names.index(CI_GATE_TASK) < names.index(COPY_TASK)
    copy = _task(CI_GATE_TASK)["ansible.builtin.copy"]
    assert copy["dest"] == f"{INSTALL_DIR}/ci_verdict.py"
    assert copy["src"].endswith("/../hosts/lib/ci_verdict.py")
    assert {
        "live": f"{INSTALL_DIR}/ci_verdict.py",
        "src": "hosts/lib/ci_verdict.py",
    } in (_stamp_pairs())


# --- The import census ---------------------------------------------------------------------


def _imported_locally(rel, local):
    """The modules in `local` that the file `rel` imports, as paths relative to files/."""
    tree = ast.parse((FILES / rel).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module == PACKAGE:
                names = [f"{PACKAGE}/{a.name}.py" for a in node.names]
            else:
                names = [f"{(node.module or '').replace('.', '/')}.py"]
            imported |= {n for n in names if n in local}
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = f"{alias.name.replace('.', '/')}.py"
                if name in local:
                    imported.add(name)
    return imported


def test_every_module_the_entrypoint_reaches_is_shipped():
    """The property that actually breaks the unit, asserted on its own.

    Set equality above already implies it; this keeps holding if the runtime/test split is
    ever loosened. It follows the imports to their closure, so it also covers what the facade
    `deploy_logic.py` re-exports: every module the entrypoint reaches has to be shipped.
    """
    shipped = set(_copy_loop())
    local = set(_runtime_modules_in(FILES))
    reached, todo = set(), {ENTRYPOINT}
    while todo:
        rel = todo.pop()
        reached.add(rel)
        todo |= _imported_locally(rel, local) - reached
    assert f"{PACKAGE}/deploy_phases.py" in reached, "the census follows no import"
    missing = reached - shipped
    assert not missing, (
        f"{ENTRYPOINT} reaches {sorted(missing)}, absent from the copy loop"
    )


def _install(tmp_path, dest_template=None, omit=()):
    """Lay the shipped files out under `tmp_path` as the role does, then run its removal."""
    for rel, live in _installed(dest_template).items():
        target = tmp_path / live.removeprefix(f"{INSTALL_DIR}/")
        target.parent.mkdir(exist_ok=True)
        shutil.copy(FILES / rel, target)
    for name in _task(REMOVE_TASK)["loop"]:
        (tmp_path / name).unlink(missing_ok=True)
    for name in (
        "host_lib.py",
        "kuma_push.py",
        *sorted(set(HOST_LIB_ONLY) - set(omit)),
    ):
        shutil.copy(HOST_LIB_FILES / name, tmp_path / name)


def _import_entrypoint(tmp_path):
    # The config path is absent on purpose: the entry point reads it at import, and an absent
    # file loads as an empty config rather than reading the host's. The insert stands in for
    # the script's own directory, which is sys.path[0] when systemd runs it by path.
    code = f"import sys; sys.path.insert(0, {str(tmp_path)!r}); import gitops_deploy"
    return subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=tmp_path,
        env={"GITOPS_DEPLOY_CONFIG": str(tmp_path / "no-config")},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_entrypoint_imports_from_the_installed_layout(tmp_path):
    """The deployer applies its own code, so a layout that fails to import parks every landing.

    `-I` drops the repo's paths, so only the install directory can resolve `deployer_lib`.
    """
    _install(tmp_path)
    proc = _import_entrypoint(tmp_path)
    assert proc.returncode == 0, proc.stderr


def test_a_dest_that_flattens_the_package_fails_to_import(tmp_path):
    _install(tmp_path, "/opt/gitops-deploy/{{ item | basename }}")
    proc = _import_entrypoint(tmp_path)
    assert "ModuleNotFoundError" in proc.stderr, proc.stderr


# --- Proof the guard can go red ------------------------------------------------------------
#
# Each rule is exercised on an input it must reject as well as the live tree it accepts. A
# guard only ever observed passing has no evidence it can fail.


def _tree(tmp_path, *names):
    for name in names:
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_text("")
    return _runtime_modules_in(tmp_path)


def test_current_shape_is_clean(tmp_path):
    runtime = _tree(
        tmp_path,
        "gitops_deploy.py",
        f"{PACKAGE}/deploy_logic.py",
        "test_deploy_health.py",
    )
    shipped = ["gitops_deploy.py", f"{PACKAGE}/deploy_logic.py"]
    assert _missing_from_copy_loop(shipped, runtime) == []
    assert _missing_from_stamp_pairs(_stamp_pairs(), runtime) == []


def test_module_missing_from_copy_loop_is_flagged(tmp_path):
    runtime = _tree(
        tmp_path,
        "gitops_deploy.py",
        f"{PACKAGE}/deploy_logic.py",
        f"{PACKAGE}/deploy_classify.py",
    )
    assert _missing_from_copy_loop(
        ["gitops_deploy.py", f"{PACKAGE}/deploy_logic.py"], runtime
    ) == [f"{PACKAGE}/deploy_classify.py"]


def test_module_missing_from_stamp_pairs_is_flagged(tmp_path):
    runtime = _tree(tmp_path, "gitops_deploy.py", f"{PACKAGE}/deploy_classify.py")
    assert _missing_from_stamp_pairs(_stamp_pairs(), runtime) == [
        f"{PACKAGE}/deploy_classify.py"
    ]


def test_a_scratch_install_without_the_ci_gate_module_fails_to_import(tmp_path):
    """The reject half: `deploy_git` imports `ci_verdict`, so the install must carry it."""
    _install(tmp_path, omit=HOST_LIB_ONLY)
    proc = _import_entrypoint(tmp_path)
    assert "No module named 'ci_verdict'" in proc.stderr, proc.stderr
