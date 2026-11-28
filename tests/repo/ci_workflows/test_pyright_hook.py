"""The `pyright` prek hook fails a commit on a pyright error and passes one with a warning.

Each test runs the hook's own `entry` from the repo root, so [tool.pyright] configures it, over
one scratch file. Pyright is in the `lint` group, which CI's pytest shards skip, so the
`pyright-red-proof` prek hook runs this module in the hooks job instead.

Run: uv run pytest tests/repo/ci_workflows/test_pyright_hook.py
"""

import shlex
import shutil
import subprocess
import tomllib

import pytest
from lib.repo_paths import REPO

needs_pyright = pytest.mark.skipif(
    shutil.which("pyright") is None,
    reason="pyright is in the lint group; the pyright-red-proof prek hook runs this",
)


def _hook_on(path) -> tuple[int, str]:
    config = tomllib.loads((REPO / "prek.toml").read_text())
    (hook,) = [
        hook
        for repo in config["repos"]
        for hook in repo.get("hooks", [])
        if hook.get("id") == "pyright"
    ]
    done = subprocess.run(
        [*shlex.split(hook["entry"]), str(path)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return done.returncode, done.stdout + done.stderr


@needs_pyright
def test_a_file_with_only_a_pyright_warning_is_clean(tmp_path):
    path = tmp_path / "warned.py"
    path.write_text("def double(x: int) -> int:\n    x\n    return 2 * x\n")
    rc, out = _hook_on(path)
    assert rc == 0, out
    assert "(reportUnusedExpression)" in out, out


@needs_pyright
def test_a_file_with_a_pyright_error_is_flagged(tmp_path):
    path = tmp_path / "wrong.py"
    path.write_text("def double(x: int) -> int:\n    return 2 * x\n\n\ndouble(None)\n")
    rc, out = _hook_on(path)
    assert rc == 1, out
    assert "(reportArgumentType)" in out, out
