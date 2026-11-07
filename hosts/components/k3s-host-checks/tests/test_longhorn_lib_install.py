"""The backup-health reader's libraries ship as `longhorn_lib/` in daniel-box's package (#4354).

The package sits in `scripts/lib/longhorn_lib/` since H7.2e, so the reapers and `probe.py` import
the same modules from the checkout. A module added there and left off the component's `shared:`
entries imports fine here and kills the `*/10` timer on the host, so these tests read the rendered
package rather than the tree.

Run: uv run pytest hosts/components/k3s-host-checks/tests/test_longhorn_lib_install.py
"""

import subprocess
import sys

import pytest

from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

INSTALL_DIR = "/usr/lib/homelab/longhorn-backup-health"
LIB = REPO / "scripts" / "lib" / "longhorn_lib"


@pytest.fixture(scope="module")
def package() -> host_mode.Package:
    return host_mode.render_host("daniel-box")


def _shipped(package) -> set[str]:
    prefix = f"{INSTALL_DIR}/longhorn_lib/"
    return {p.removeprefix(prefix) for p in package.files if p.startswith(prefix)}


def _missing(package, lib_modules: set[str]) -> set[str]:
    return lib_modules - _shipped(package)


def test_every_lib_module_ships_beside_the_reader(package):
    lib_modules = {p.name for p in LIB.glob("*.py")}
    # Named members, so a move that empties the glob fails here rather than passing on nothing.
    assert {"longhorn_backups.py", "longhorn_backup_health_logic.py"} <= lib_modules
    assert _missing(package, lib_modules) == set()
    assert f"{INSTALL_DIR}/longhorn_backup_health.py" in package.files
    assert f"{INSTALL_DIR}/host_lib.py" in package.files


def test_a_module_left_off_the_package_is_flagged(package):
    assert _missing(package, {"longhorn_not_shipped.py"}) == {"longhorn_not_shipped.py"}


def test_the_reader_imports_from_the_installed_layout(package, tmp_path):
    for path, (body, *_rest) in package.files.items():
        if not path.startswith(f"{INSTALL_DIR}/"):
            continue
        target = tmp_path / path.removeprefix(f"{INSTALL_DIR}/")
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(body if isinstance(body, bytes) else body.encode())
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, {str(tmp_path)!r}); "
            "import host_lib; from longhorn_lib import longhorn_backups",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
