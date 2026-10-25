"""The backup-health reader's libraries ship as `longhorn_lib/` and the old flat copies go (#4354).

`tasks/health-crons.yml` copies the reader and its package into /opt/longhorn-backup-health/
one file at a time. The package sits in `scripts/lib/longhorn_lib/` since H7.2e, so the
reapers and `probe.py` import the same modules from the checkout. A module added there and left
off that loop imports fine here and kills the `*/10` cron on the host, so these tests read the
loop rather than the tree.
They also render the task's `dest:` for each item, because a `dest` that flattens the package
puts the libraries exactly where the removal task deletes them.

Run: uv run pytest ansible/roles/setup/k3s/tests/test_longhorn_lib_install.py
"""

import shutil
import subprocess
import sys

from lib import yaml_fast
from lib.ansible_jinja_env import make_ansible_env
from lib.repo_paths import HOST_LIB_FILES, K3S_FILES, K3S_ROLE, REPO

INSTALL_DIR = "/opt/longhorn-backup-health"
LIB = REPO / "scripts" / "lib" / "longhorn_lib"
LIB_SRC = "scripts/lib/longhorn_lib"
READER_SRC = "ansible/roles/setup/k3s/files/longhorn_backup_health.py"


def _tasks() -> list[dict]:
    return yaml_fast.safe_load((K3S_ROLE / "tasks" / "health-crons.yml").read_text())


def _task(name: str) -> dict:
    return next(t for t in _tasks() if t.get("name") == name)


def _lib_task() -> dict:
    return _task("Install the Longhorn backup health reader libraries")


def _reader_task() -> dict:
    return _task("Install the Longhorn backup health reader scripts")


def _installed() -> dict[str, str]:
    """Each shipped file's repo path, mapped to the path its copy task's `dest:` renders.

    Rendered from the tasks rather than built from the loop item. A `dest` that flattens the
    package (`{{ item | basename }}`) would put the libraries where the removal task below
    deletes them, and only the rendered path shows that.
    """
    env = make_ansible_env()
    lib_dest = env.from_string(_lib_task()["ansible.builtin.copy"]["dest"])
    installed = {
        f"{LIB_SRC}/{item}": lib_dest.render(item=item) for item in _lib_task()["loop"]
    }
    reader_dest = _reader_task()["ansible.builtin.copy"]["dest"]
    installed[READER_SRC] = reader_dest
    return installed


def test_every_lib_module_is_shipped_before_the_reader():
    shipped = _lib_task()["loop"]
    lib_modules = {p.name for p in LIB.glob("*.py")}
    # Named members, so a move that empties the glob fails here rather than passing on nothing.
    assert {"longhorn_backups.py", "longhorn_backup_health_logic.py"} <= lib_modules
    assert lib_modules <= set(shipped)
    assert _lib_task()["ansible.builtin.copy"]["src"].endswith(
        "/../scripts/lib/longhorn_lib/{{ item }}"
    )
    names = [t.get("name") for t in _tasks()]
    assert names.index(_lib_task()["name"]) < names.index(_reader_task()["name"]), (
        "the reader must land after its imports"
    )


def test_a_lib_module_left_off_the_loop_would_be_flagged():
    """The reject half: the census above is a subset test, so a short loop must fail it."""
    short = set(_lib_task()["loop"]) - {"longhorn_backups.py"}
    lib_modules = {p.name for p in LIB.glob("*.py")}
    assert not lib_modules <= short


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
    for src, live in _installed().items():
        assert (live, src) in pairs, src


def test_the_copy_keeps_the_package_directory_on_the_host():
    installed = _installed()
    assert (
        installed[f"{LIB_SRC}/longhorn_backups.py"]
        == f"{INSTALL_DIR}/longhorn_lib/longhorn_backups.py"
    )
    assert installed[READER_SRC] == f"{INSTALL_DIR}/longhorn_backup_health.py"


def test_the_reader_imports_from_the_installed_layout(tmp_path):
    # Each file goes where the copy task's rendered `dest:` puts it, re-rooted at tmp_path, with
    # host_lib and kuma_push beside the reader as install_host_lib.yml puts them. The removal
    # task then runs too, so a flattening `dest` leaves the package empty here as on the host.
    for src, live in _installed().items():
        target = tmp_path / live.removeprefix(f"{INSTALL_DIR}/")
        target.parent.mkdir(exist_ok=True)
        shutil.copy(REPO / src, target)
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
