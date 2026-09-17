"""Tests for `_claude_guard.py`, the bootstrap onto the deployed `claude_guard` package.

This is slice 5's narrowed exit criterion (docs/specs/2026-09-06-claude-guard-design.md,
dotfiles repo): `.claude/hooks/_readonly_tables.py` now imports `SSH_HOSTS` and `_SSH_SECRET`
from `claude_guard.tables` rather than carrying its own copies. The deployed-import test was
red before this change — nothing in this repo's `uv` environment could import `claude_guard`
at all, so the two tables could only ever be duplicates, not the same object.

Run: uv run pytest .claude/hooks
"""

import importlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent
REPO = (
    HOOKS.parent.parent
)  # .claude/hooks/tests -> .claude/hooks -> .claude -> repo root

sys.path.insert(0, str(HOOKS))  # _readonly_tables imports _claude_guard by bare name

import _readonly_tables  # noqa: E402
import claude_guard.tables as _tables  # noqa: E402

# Same hardcoded path and reasoning as test_auto_approve_remote_ssh.py:40-46: `uv` missing
# is one reason to skip. The other is the real deploy target itself: this test spawns a
# SEPARATE subprocess, which starts its own sys.modules and never sees conftest.py's
# in-process stand-in, so on a host that genuinely lacks the dotfiles deploy it can only ever
# fail, not prove anything — the same reasoning as the e2e wrapper tests below.
UV_BIN = Path("/home/ubuntu/.local/bin/uv")
_CLAUDE_GUARD_DIR = Path("~/.local/share/claude-guard").expanduser()

_runnable = pytest.mark.skipif(
    not (UV_BIN.exists() and _CLAUDE_GUARD_DIR.is_dir()),
    reason="uv or the deployed claude_guard package is not present on this machine",
)


@_runnable
def test_deployed_import_reaches_both_trusted_hosts():
    """Run the real invocation path: `uv run python`, hooks dir on the path via PYTHONPATH.

    This is the shape `auto-approve-remote-ssh.sh` actually runs under — `cd
    /home/ubuntu/server && exec uv run --no-sync --quiet python <hooks-dir>/<script>.py`,
    which puts the hooks dir at `sys.path[0]` because that is where the invoked script lives.
    `python -c` has no script file, so `sys.path[0]` is the cwd instead; PYTHONPATH is what
    puts the hooks dir on the path for this invocation.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(HOOKS)
    proc = subprocess.run(
        [
            str(UV_BIN),
            "run",
            "--no-sync",
            "--quiet",
            "python",
            "-c",
            "import _claude_guard, claude_guard.tables as t; "
            "print(sorted(t.TRUSTED_SSH_HOSTS))",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "daniel-pi" in proc.stdout
    assert "daniel-server" in proc.stdout


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


def test_readonly_tables_ssh_objects_are_claude_guards_own():
    """Identity, not just equal value — a future local redefinition breaks this, not `==`.

    This pins the WIRING, not the values: wherever conftest.py's stand-in is in play (every
    CI run, since CI has no dotfiles deploy), `_tables` IS that stand-in, so the assertion is
    `x is x` regardless of what the real `claude_guard.tables` holds. Only a run with the real
    package deployed exercises the values this identity check is meant to protect.
    """
    assert _readonly_tables.SSH_HOSTS == frozenset(_tables.TRUSTED_SSH_HOSTS)
    assert _readonly_tables._SSH_SECRET is _tables.SECRET_PATH_RE
