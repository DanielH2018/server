"""`lib.host_modules` makes the host-side modules importable by bare name.

Each test runs a fresh `-S` interpreter with only `scripts/` on `PYTHONPATH`, because pytest's
own `pythonpath` already lists both directories and would pass an in-process check for nothing.

Run: uv run pytest scripts/lib/tests/script_census/test_host_modules.py
"""

import os
import subprocess
import sys

from lib.repo_paths import ROLE_MODULES, SCRIPTS


def _imports(code: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(SCRIPTS)}
    return subprocess.run(
        [sys.executable, "-S", "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def test_the_host_modules_import_after_it_is_clean():
    proc = _imports(
        "from lib import host_modules\n"
        "import host_lib, ci_verdict, builder_ci_grade, gh_quota\n"
    )
    assert proc.returncode == 0, proc.stderr


def test_every_listed_role_module_imports_after_it():
    names = [name for names in ROLE_MODULES.values() for name in names]
    proc = _imports(f"from lib import host_modules\nimport {', '.join(names)}\n")
    assert names and proc.returncode == 0, proc.stderr


def test_a_host_module_without_it_is_flagged():
    proc = _imports("import host_lib\n")
    assert "ModuleNotFoundError" in proc.stderr
