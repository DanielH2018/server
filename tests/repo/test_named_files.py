"""A run that names a file builds no node for that file's siblings.

Each run is a real pytest subprocess over a scratch tree, with a bare `pytest.ini` so the
repo's own `addopts` stay out. The scratch conftest prints every file that reaches
`pytest_collect_file`, which is where pytest builds a module node.

Run: uv run pytest tests/repo/test_named_files.py
"""

import os
import subprocess
import sys
from pathlib import Path

from _helpers import REPO


TESTS = REPO / "tests"

_CONFTEST = """
def pytest_collect_file(file_path, parent):
    print("REACHED", file_path.name)
"""


def _reached(tmp_path: Path, *args: str) -> list[str]:
    suite = tmp_path / "suite"
    suite.mkdir()
    for name in ("a", "b", "c"):
        (suite / f"test_{name}.py").write_text(f"def test_{name}():\n    pass\n")
    (tmp_path / "conftest.py").write_text(_CONFTEST)
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env["PYTHONPATH"] = str(TESTS)
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "named_files", "-s", *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return sorted(
        {
            line.split()[1]
            for line in done.stdout.splitlines()
            if line.startswith("REACHED ")
        }
    )


def test_the_siblings_of_named_files_never_reach_collect_file(tmp_path: Path) -> None:
    reached = _reached(tmp_path, "suite/test_a.py", "suite/test_b.py")
    assert reached == ["test_a.py", "test_b.py"]


def test_a_named_directory_still_collects_every_file_in_it(tmp_path: Path) -> None:
    reached = _reached(tmp_path, "suite")
    assert reached == ["test_a.py", "test_b.py", "test_c.py"]
