"""Tests for `_claude_worktree.py`, the bootstrap onto the deployed `claude_worktree` module.

`lib.worktrees` imports the library through it. What has to hold: a missing deploy, or one
older than this repo was written for, raises rather than falling back to a stale copy; and
the env var names the copy that gets imported. CI links a pinned dotfiles checkout into the
deployed path, so every test here runs there too.

Run: uv run pytest scripts/lib/tests/script_census/test_claude_worktree_import.py
"""

import os
import sys
from pathlib import Path

# The bootstrap, imported rather than read out of sys.modules, so a host or runner with no
# deploy fails collection here with the bootstrap's own message.
from lib import _claude_worktree  # noqa: F401
import claude_worktree
from lib.proc_testing import run

SCRIPTS = Path(__file__).resolve().parents[3]
# Wherever the bootstrap found it: the deploy, or what CLAUDE_WORKTREE_HOME named.
DEPLOYED = Path(claude_worktree.__file__ or "")


def _without_deploy(tmp_path):
    """An environment in which no `claude_worktree` is reachable, in-process state aside."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["HOME"] = str(tmp_path)
    env["CLAUDE_WORKTREE_HOME"] = str(tmp_path / "absent")
    return env


def test_bootstrap_raises_when_the_deploy_is_missing(tmp_path):
    """No deploy means `_claude_worktree` raises and names the fix — not a stale fallback.

    A subprocess rather than an in-process import: this module has already imported
    `claude_worktree`, and the env var is read once at import.
    """
    proc = run(
        [sys.executable, "-c", "import lib._claude_worktree"],
        cwd=SCRIPTS,
        env=_without_deploy(tmp_path),
        check=False,
    )
    assert proc.returncode != 0
    assert "ImportError" in proc.stderr
    assert str(tmp_path / "absent") in proc.stderr
    assert "chezmoi apply" in proc.stderr


def test_bootstrap_raises_when_the_deploy_is_older_than_this_repo_needs(tmp_path):
    # A copy from before the library carried `INTERFACE` reads as interface 1.
    stale = tmp_path / "stale"
    stale.mkdir()
    (stale / "claude_worktree.py").write_text('"""An old copy."""\n')
    proc = run(
        [sys.executable, "-c", "import lib._claude_worktree"],
        cwd=SCRIPTS,
        env={**_without_deploy(tmp_path), "CLAUDE_WORKTREE_HOME": str(stale)},
        check=False,
    )
    assert proc.returncode != 0
    assert "is interface 1 and this repo needs" in proc.stderr
    assert "chezmoi apply" in proc.stderr


def test_bootstrap_imports_the_module_the_env_var_names(tmp_path):
    proc = run(
        [
            sys.executable,
            "-c",
            "import lib._claude_worktree, claude_worktree; print(claude_worktree.__file__)",
        ],
        cwd=SCRIPTS,
        env={**_without_deploy(tmp_path), "CLAUDE_WORKTREE_HOME": str(DEPLOYED.parent)},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(DEPLOYED)


def test_the_worktree_library_imports_with_only_scripts_on_the_path(tmp_path):
    """`backlog.py` and `findings_lib` load the library as `lib.worktrees`.

    That puts `scripts/` on `sys.path` but not `scripts/lib/`, so a bare sibling import that
    works when the module's own directory is on the path raises here instead. A bare `import
    _claude_worktree` does exactly that, and the docs-refresh cron's `backlog.py` generator
    would fail on every run unless it is spelled `from lib import _claude_worktree`.
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["CLAUDE_WORKTREE_HOME"] = str(DEPLOYED.parent)
    code = f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); import lib.worktrees"
    done = run([sys.executable, "-c", code], cwd=tmp_path, env=env)
    assert done.returncode == 0, done.stderr
