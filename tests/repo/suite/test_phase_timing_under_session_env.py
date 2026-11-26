"""`tests/repo/suite/test_phase_timing.py` passes in the environment a daniel-box session runs in.

The Claude session units on daniel-box export `PYTHONDONTWRITEBYTECODE=1`
(`hosts/components/claude-code/templates/claude-bg-resume.service.j2` among them), and every
pytest a session starts inherits it. The phase timing tests run a scratch pytest twice and
expect the second run to reuse the first run's rewritten-assertion cache (#8920).

Run: uv run pytest tests/repo/suite/test_phase_timing_under_session_env.py
"""

import os
import subprocess
import sys

from _helpers import REPO


CACHED_RERUN = (
    "test_a_run_counts_the_modules_it_rewrote_and_a_cached_rerun_rewrites_none"
)


def test_the_phase_timing_file_passes_with_bytecode_writing_disabled():
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            "-n0",
            "-rA",
            "tests/repo/suite/test_phase_timing.py",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    out = done.stdout + done.stderr
    assert done.returncode == 0, out
    assert f"PASSED tests/repo/suite/test_phase_timing.py::{CACHED_RERUN}" in out, out
