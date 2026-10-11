"""The fan-out entry points a unit runs under `uv run --no-project` import with the stdlib alone.

`launch.review_command` runs `fanout.py review`, and `clean`'s remote leg runs
`fanout_place.py clean-one`, both through `uv run --no-project`, which installs nothing. A
module-level import of PyYAML anywhere under them killed every review unit and every remote
clean at import, behind a green suite whose venv has PyYAML (#4167). `python -S` skips
site-packages, which is the same interpreter with no third-party package on the path. The
`fanout_review.py` shim is what an older checkout's launch still runs in a batch worktree.

Run: uv run pytest scripts/dev/tests/test_fanout_headless_imports.py
"""

import subprocess
import sys
from pathlib import Path

import pytest

DEV = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "argv",
    [
        ["fanout.py", "review", "--help"],
        ["fanout_review.py", "--help"],
        ["fanout_place.py", "clean-one", "--help"],
    ],
    ids=["review-unit", "review-unit-old-path", "clean-remote-leg"],
)
def test_a_headless_entry_point_imports_without_site_packages(argv):
    done = subprocess.run(
        [sys.executable, "-S", str(DEV / argv[0]), *argv[1:]],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
