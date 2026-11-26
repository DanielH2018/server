"""The fan-out entry points a unit runs under `uv run --no-project` import with the stdlib alone.

`launch.review_command` runs `fanout.py review`, and `clean`'s remote leg runs
`fanout.py place clean-one`, both through `uv run --no-project`, which installs nothing. A
module-level import of PyYAML anywhere under them killed every review unit and every remote
clean at import, behind a green suite whose venv has PyYAML (#4167). `python -S` skips
site-packages, which is the same interpreter with no third-party package on the path. Each
launcher sets `PYTHONPATH` to its checkout's `scripts/`, so the run here does too.

Run: uv run pytest scripts/dev/tests/fanout/test_fanout_headless_imports.py
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

DEV = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "argv",
    [
        ["fanout.py", "review", "--help"],
        ["fanout.py", "place", "clean-one", "--help"],
        ["fanout_lib/notify.py", "--help"],
    ],
    ids=["review-unit", "clean-remote-leg", "stop-notifier"],
)
def test_a_headless_entry_point_imports_without_site_packages(argv):
    done = subprocess.run(
        [sys.executable, "-S", str(DEV / argv[0]), *argv[1:]],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ, "PYTHONPATH": str(DEV.parent)},
    )
    assert done.returncode == 0, done.stderr[-2000:]
