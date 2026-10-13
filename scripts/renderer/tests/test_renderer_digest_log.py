"""The determinism modules' digest lines reach the output of an xdist run.

CI runs every shard under xdist, where a test runs in a worker and only the controller writes
the job log. The test runs a pytest session of its own under `-n 2` with this directory's real
`conftest.py`, so the lines take the same path out of a worker as they do in CI.

Run: uv run pytest scripts/renderer/tests/test_renderer_digest_log.py
"""

import os
import shutil
import sys
from pathlib import Path

from _render_audit import DIGEST_PROPERTY
from lib.proc_testing import run
from lib.repo_paths import SCRIPTS

HERE = Path(__file__).resolve().parent

INNER_TESTS = f"""
def test_a_part_records_its_lines(record_property):
    record_property(
        {DIGEST_PROPERTY!r},
        {{"sha": "f00", "module": "position 2 of 4", "lines": ["littlelink sha256:ab"]}},
    )


def test_another_property_is_not_a_digest(record_property):
    record_property("unrelated", {{"sha": "f00", "module": "m", "lines": ["flux sha256:cd"]}})
"""


def test_a_workers_digest_lines_print_once_in_the_session_output(tmp_path):
    shutil.copy(HERE / "conftest.py", tmp_path / "conftest.py")
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_inner.py").write_text(INNER_TESTS)
    done = run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-n",
            "2",
            "-p",
            "no:cacheprovider",
            "-c",
            str(tmp_path / "pytest.ini"),
            str(tmp_path),
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": os.pathsep.join([str(HERE), str(SCRIPTS)])},
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "2 workers" in done.stdout, done.stdout
    # Once, though the teardown report carries the same property as the call report.
    assert done.stdout.count("render digest at") == 1, done.stdout
    assert "render digest at f00 (position 2 of 4): littlelink sha256:ab" in done.stdout
