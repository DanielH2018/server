"""Tests for `_claude_guard.py`, the bootstrap onto the deployed `claude_guard` package.

`_hook_common.py` imports `claude_guard.segment` through it, and every Bash guard here cuts
its stages with that segmenter. The read-only classifier that also read `claude_guard.tables`
through it moved into the package itself (dotfiles #628), along with the stand-in, boundary
and shared-verdict tests that compared the two copies.

Run: uv run pytest .claude/hooks
"""

import importlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent
REPO = (
    HOOKS.parent.parent
)  # .claude/hooks/tests -> .claude/hooks -> .claude -> repo root

sys.path.insert(0, str(HOOKS))  # _claude_guard is imported by bare name

# The `uv` that is running this suite: `uv run` exports its own path as `UV`, and PATH is
# the fallback for a bare `pytest`. Resolved at import, before leakguard swaps PATH for a
# stub directory at each test's setup. It was `/home/ubuntu/.local/bin/uv` until #2159,
# which was a fact about one host rather than about the tree.
UV_BIN = os.environ.get("UV") or shutil.which("uv") or ""
_CLAUDE_GUARD_DIR = Path("~/.local/share/claude-guard").expanduser()

# A separate interpreter never sees this process's sys.modules, so the deployed-import test
# can only fail, not prove anything, on a host that lacks the dotfiles deploy.
_runnable = pytest.mark.skipif(
    not UV_BIN or not _CLAUDE_GUARD_DIR.is_dir(),
    reason="no `uv` on PATH to spawn"
    if not UV_BIN
    else f"the deployed claude_guard package is not present at {_CLAUDE_GUARD_DIR}",
)


@_runnable
def test_deployed_import_reaches_the_segmenter():
    """Run the real invocation path: `uv run python`, hooks dir on the path via PYTHONPATH.

    This is the shape `bash-pretool.sh` actually runs under — `cd
    /home/ubuntu/server && exec uv run --no-sync --quiet python <hooks-dir>/<script>.py`,
    which puts the hooks dir at `sys.path[0]` because that is where the invoked script lives.
    `python -c` has no script file, so `sys.path[0]` is the cwd instead; PYTHONPATH is what
    puts the hooks dir on the path for this invocation.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(HOOKS)
    proc = subprocess.run(
        [
            UV_BIN,
            "run",
            "--no-sync",
            "--quiet",
            "python",
            "-c",
            "import _claude_guard, claude_guard.segment as s; "
            "print([x.text.strip() for x in s.parse('ls; pwd').segments])",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "['ls', 'pwd']"


def test_bootstrap_raises_when_the_deploy_is_missing(tmp_path, monkeypatch):
    """No `~/.local/share/claude-guard` means `_claude_guard` raises, not a stale fallback.

    Points HOME at an empty tmp dir, then strips every trace of an already-imported
    `claude_guard`/`_claude_guard` (sys.modules entries and any sys.path entry the real
    bootstrap added) so a prior successful import in this process can't mask the failure.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    saved_path = list(sys.path)
    saved_modules = {
        name: mod
        for name, mod in sys.modules.items()
        if name == "_claude_guard" or name.startswith("claude_guard")
    }
    for name in saved_modules:
        del sys.modules[name]
    sys.path[:] = [p for p in sys.path if not p.rstrip("/").endswith("claude-guard")]
    sys.path.insert(0, str(HOOKS))
    try:
        with pytest.raises(ImportError, match=re.escape(str(tmp_path))):
            importlib.import_module("_claude_guard")
    finally:
        sys.path[:] = saved_path
        for name in list(sys.modules):
            if name == "_claude_guard" or name.startswith("claude_guard"):
                del sys.modules[name]
        sys.modules.update(saved_modules)
