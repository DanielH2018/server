"""The guard env registers `py_table` only while a tracked template calls it (#7840).

Run: uv run pytest scripts/lib/tests/test_guard_jinja_env_py_table_retired.py
"""

import re
import sys

from lib import guard_jinja_env as aje
from lib.proc_testing import run
from lib.repo_paths import REPO, SHARED_TPL

_TEMPLATE_GLOBS = ("*.j2", "*.yml")


def _templates_calling_py_table() -> list[str]:
    tracked = run(
        ["git", "ls-files", "-z", "--", *_TEMPLATE_GLOBS], cwd=REPO, check=True
    ).stdout.split("\0")
    return [
        path
        for path in tracked
        if path
        and re.search(r"\bpy_table\b", (REPO / path).read_text(errors="replace"))
    ]


def test_the_guard_env_registers_py_table_only_if_a_template_calls_it():
    callers = _templates_calling_py_table()
    registered = (
        "py_table" in aje.register_ansible_filters(aje.make_env([SHARED_TPL])).filters
    )
    assert registered == bool(callers), (
        f"py_table registered={registered}, tracked templates calling it: {callers}"
    )


def test_building_the_guard_env_does_not_load_the_py_table_module():
    """A subprocess, because other tests in the session import `py_table` directly."""
    proc = run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'scripts');"
            " from lib.guard_jinja_env import make_env, register_ansible_filters;"
            " register_ansible_filters(make_env());"
            " print('py_table' in sys.modules)",
        ],
        cwd=REPO,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False"
