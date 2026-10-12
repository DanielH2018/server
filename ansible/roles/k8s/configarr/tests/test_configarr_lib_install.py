"""The health reader's libraries ship as `configarr_lib/` and the old flat copies go (#4416).

`tasks/main.yml` copies the package into /opt/configarr-health/configarr_lib/, then the reader,
then deletes the flat copies the layout before #4416 left there. A cron runs the reader every
10 minutes, so the order is part of the contract: a removal ahead of the reader copy leaves the
old reader importing files that are gone, and that pages. These tests read the tasks rather
than the tree.

Run: uv run pytest ansible/roles/k8s/configarr/tests/test_configarr_lib_install.py
"""

import shutil
import subprocess
import sys

from lib import yaml_fast
from lib.ansible_jinja_env import make_ansible_env
from lib.repo_paths import HOST_LIB_FILES, ROLES

ROLE = ROLES / "k8s" / "configarr"
FILES = ROLE / "files"
LIB = FILES / "configarr_lib"
INSTALL_DIR = "/opt/configarr-health"

_LIB_COPY = "Install the configarr health reader library"
_READER_COPY = "Install the configarr health reader script"
_REMOVAL = "Remove the configarr health modules that moved into configarr_lib"
# The flat files the layout before #4416 left in INSTALL_DIR. A literal, not the removal
# task's loop: seeded from that loop, a name dropped from it is neither seeded nor removed,
# and the closing glob passes on a stale copy (#4441).
_PRE_4416_FLAT = ("configarr_health_logic.py", "configarr_status.py")


def _tasks() -> list[dict]:
    return yaml_fast.safe_load((ROLE / "tasks" / "main.yml").read_text())


def _index(name: str) -> int:
    return next(i for i, t in enumerate(_tasks()) if t.get("name") == name)


def _task(name: str) -> dict:
    return _tasks()[_index(name)]


def _installed_libs() -> dict[str, str]:
    """Each shipped lib module's name, mapped to the path the copy task's `dest:` renders."""
    dest = make_ansible_env().from_string(
        _task(_LIB_COPY)["ansible.builtin.copy"]["dest"]
    )
    return {name: dest.render(item=name) for name in _task(_LIB_COPY)["loop"]}


def test_every_lib_module_is_shipped_into_the_package_directory():
    lib_modules = {p.name for p in LIB.glob("*.py")}
    # Named members, so a move that empties the glob fails here rather than passing on nothing.
    assert {"configarr_health_logic.py", "configarr_status.py"} <= lib_modules
    installed = _installed_libs()
    assert set(installed) == lib_modules
    for name, live in installed.items():
        assert live == f"{INSTALL_DIR}/configarr_lib/{name}", live


def test_the_flat_copies_go_only_after_the_new_reader_is_in_place():
    assert _index(_LIB_COPY) < _index(_READER_COPY) < _index(_REMOVAL), (
        "lib, then reader, then removal: any other order leaves the cron a reader whose "
        "imports are missing"
    )


def test_the_removed_flat_files_are_the_ones_that_moved():
    removed = _task(_REMOVAL)["loop"]
    assert sorted(removed) == ["configarr_health_logic.py", "configarr_status.py"]
    for name in removed:
        assert not (FILES / name).exists(), (
            f"{name} still ships flat, so removing it breaks the reader"
        )
        assert (LIB / name).is_file(), (
            f"{name} is not in configarr_lib/, so nothing replaced it"
        )


def test_the_reader_imports_from_the_installed_layout(tmp_path):
    # The old flat layout first, then the role's copies and its removal, re-rooted at tmp_path,
    # with host_lib and kuma_push beside the reader as install_host_lib.yml puts them.
    for name in _PRE_4416_FLAT:
        (tmp_path / name).write_text("raise ImportError('stale flat copy')\n")
    for name, live in _installed_libs().items():
        target = tmp_path / live.removeprefix(f"{INSTALL_DIR}/")
        target.parent.mkdir(exist_ok=True)
        shutil.copy(LIB / name, target)
    shutil.copy(FILES / "configarr_health.py", tmp_path / "configarr_health.py")
    for name in _task(_REMOVAL)["loop"]:
        (tmp_path / name).unlink()
    for name in ("host_lib.py", "kuma_push.py"):
        shutil.copy(HOST_LIB_FILES / name, tmp_path / name)
    # `--help` returns after every module-level import has run, so exit 0 proves the import
    # closure resolved from this layout alone.
    proc = subprocess.run(
        [sys.executable, "-I", str(tmp_path / "configarr_health.py"), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert sorted(p.name for p in tmp_path.glob("configarr_*.py")) == [
        "configarr_health.py"
    ], "a flat lib copy survived"
