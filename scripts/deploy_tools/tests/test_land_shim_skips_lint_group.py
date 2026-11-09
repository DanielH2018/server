"""test_the_shim_runs_from_any_cwd must not install the `lint` group (#7788).

`actionlint-py` is in `lint`, ships only an sdist, and its build downloads the actionlint binary
from GitHub releases with no cache. CI runs the suite with `UV_NO_GROUP=lint`, so a nested
`uv run` that resolves the default groups builds it, and a release-host outage fails the test.

This runs that test from a copy of the tracked tree, so the nested `uv run` syncs a fresh
`.venv` the way CI's does, under a uv user config that is offline and refuses to build
`actionlint-py`: the stand-in for "the lint group is absent from the cache and its release
host is down".

Run: uv run pytest scripts/deploy_tools/tests/test_land_shim_skips_lint_group.py
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from lib.repo_paths import REPO as _REPO


_SHIM_TEST = (
    "scripts/deploy_tools/tests/test_land_shim.py::test_the_shim_runs_from_any_cwd"
)
_UV_TOML = 'offline = true\nno-build-package = ["actionlint-py"]\n'


def _copy_tracked_tree(dest: Path) -> None:
    listed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    for rel in filter(None, listed.split("\0")):
        src = _REPO / rel
        if not src.is_file():
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)


def test_the_shim_test_passes_without_building_the_lint_group(tmp_path):
    tree = tmp_path / "tree"
    _copy_tracked_tree(tree)
    config = tmp_path / "config"
    (config / "uv").mkdir(parents=True)
    (config / "uv" / "uv.toml").write_text(_UV_TOML)

    env = {**os.environ, "XDG_CONFIG_HOME": str(config), "UV_NO_GROUP": "lint"}
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            _SHIM_TEST,
            "-n0",
            "-p",
            "no:cacheprovider",
            "-q",
        ],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )

    assert "actionlint" not in r.stdout, r.stdout[-4000:]
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
