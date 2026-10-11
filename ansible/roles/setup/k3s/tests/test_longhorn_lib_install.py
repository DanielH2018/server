"""The backup-health reader's libraries ship as `longhorn_lib/` and the old flat copies go (#4354).

`tasks/health-crons.yml` copies the reader and its package into /opt/longhorn-backup-health/
one file at a time. A module added to `files/longhorn_lib/` and left off that loop imports fine
here and kills the `*/10` cron on the host, so these tests read the loop rather than the tree.
They also render the task's `dest:` for each item, because a `dest` that flattens the package
puts the libraries exactly where the removal task deletes them.

Run: uv run pytest ansible/roles/setup/k3s/tests/test_longhorn_lib_install.py
"""

import shutil
import subprocess
import sys

from lib import yaml_fast
from lib.ansible_jinja_env import make_ansible_env
from lib.repo_paths import HOST_LIB_FILES, K3S_FILES, K3S_ROLE

INSTALL_DIR = "/opt/longhorn-backup-health"
LIB = K3S_FILES / "longhorn_lib"


def _tasks() -> list[dict]:
    return yaml_fast.safe_load((K3S_ROLE / "tasks" / "health-crons.yml").read_text())


def _task(name: str) -> dict:
    return next(t for t in _tasks() if t.get("name") == name)


def _copy_task() -> dict:
    return _task("Install the Longhorn backup health reader scripts")


def _shipped() -> list[str]:
    return _copy_task()["loop"]


def _installed() -> dict[str, str]:
    """Each shipped file's source path, mapped to the path the copy task's `dest:` renders.

    Rendered from the task rather than built from the loop item. A `dest` that flattens the
    package (`{{ item | basename }}`) would put the libraries where the removal task below
    deletes them, and only the rendered path shows that.
    """
    dest = make_ansible_env().from_string(_copy_task()["ansible.builtin.copy"]["dest"])
    return {rel: dest.render(item=rel) for rel in _shipped()}


def test_every_lib_module_is_shipped_before_the_reader():
    shipped = _shipped()
    lib_modules = {f"longhorn_lib/{p.name}" for p in LIB.glob("*.py")}
    # Named members, so a move that empties the glob fails here rather than passing on nothing.
    assert {
        "longhorn_lib/longhorn_backups.py",
        "longhorn_lib/longhorn_backup_health_logic.py",
    } <= lib_modules
    assert lib_modules <= set(shipped)
    assert shipped[-1] == "longhorn_backup_health.py", (
        "the reader must land after its imports"
    )


def test_the_removed_flat_files_are_ones_the_role_no_longer_ships():
    removed = _task("Remove the reader libraries that moved into longhorn_lib")["loop"]
    assert len(removed) == 5
    for name in removed:
        assert not (K3S_FILES / name).exists(), (
            f"{name} still ships flat, so removing it breaks the reader"
        )
        assert (LIB / name).is_file(), (
            f"{name} is not in longhorn_lib/, so nothing replaced it"
        )


def test_every_shipped_file_is_stamped_at_its_installed_path():
    stamped = _task("Record the deployed Longhorn backup health reader")["vars"][
        "stamp_deployed_pairs"
    ]
    pairs = {(p["live"], p["src"]) for p in stamped}
    for rel, live in _installed().items():
        assert (live, f"ansible/roles/setup/k3s/files/{rel}") in pairs, rel


def test_the_copy_keeps_the_package_directory_on_the_host():
    installed = _installed()
    assert (
        installed["longhorn_lib/longhorn_backups.py"]
        == f"{INSTALL_DIR}/longhorn_lib/longhorn_backups.py"
    )
    assert (
        installed["longhorn_backup_health.py"]
        == f"{INSTALL_DIR}/longhorn_backup_health.py"
    )


def test_the_reader_imports_from_the_installed_layout(tmp_path):
    # Each file goes where the copy task's rendered `dest:` puts it, re-rooted at tmp_path, with
    # host_lib and kuma_push beside the reader as install_host_lib.yml puts them. The removal
    # task then runs too, so a flattening `dest` leaves the package empty here as on the host.
    for rel, live in _installed().items():
        target = tmp_path / live.removeprefix(f"{INSTALL_DIR}/")
        target.parent.mkdir(exist_ok=True)
        shutil.copy(K3S_FILES / rel, target)
    for name in _task("Remove the reader libraries that moved into longhorn_lib")[
        "loop"
    ]:
        (tmp_path / name).unlink(missing_ok=True)
    for name in ("host_lib.py", "kuma_push.py"):
        shutil.copy(HOST_LIB_FILES / name, tmp_path / name)
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            f"import sys; sys.path.insert(0, {str(tmp_path)!r}); import longhorn_backup_health",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # The reader reads the shim's env at import, after every import above it has run, so
    # reaching that refusal is proof the whole import closure resolved from this layout.
    assert "ModuleNotFoundError" not in proc.stderr, proc.stderr
    assert "required env var LONGHORN_BACKUP_NAMESPACE is not set" in proc.stderr, (
        proc.stderr
    )
