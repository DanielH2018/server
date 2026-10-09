"""Tests for `_claude_worktree.py`, the bootstrap onto the deployed `claude_worktree` module.

`lib.worktrees` imports its readers through it, and `prune_worktrees.py` imports
`lib.worktrees`. What has to hold: a missing
deploy raises, and removes nothing, rather than falling back to a stale copy; and the env
var names the copy that gets imported. CI links a pinned dotfiles checkout into the deployed
path, so every test here runs there too.

Run: uv run pytest scripts/lib/tests/test_claude_worktree_import.py
"""

import os
import sys
from pathlib import Path

# The bootstrap, imported rather than read out of sys.modules, so a host or runner with no
# deploy fails collection here with the bootstrap's own message.
from lib import _claude_worktree  # noqa: F401
import claude_worktree
from lib.proc_testing import run

SCRIPTS = Path(__file__).resolve().parents[2]
REPO = SCRIPTS.parent
PRUNER = SCRIPTS / "dev" / "prune_worktrees.py"
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


def test_prune_removes_nothing_when_the_deploy_is_missing(tmp_path):
    """The fail direction is KEEP: `--prune` without the readers reports and removes nothing.

    It exits non-zero with the bootstrap's message, so the SessionStart banner's `--brief`
    call (which prints nothing on a healthy day) cannot read the failure as "nothing to
    remove" — the banner sees the exit and says detection is broken.
    """
    proc = run(
        [sys.executable, str(PRUNER), "--prune"],
        cwd=REPO,
        env=_without_deploy(tmp_path),
        check=False,
    )
    assert proc.returncode != 0
    assert "removed " not in proc.stdout
    assert "chezmoi apply" in proc.stderr


def test_gc_fails_loudly_rather_than_repairing_without_the_deploy(tmp_path):
    """`--gc` needs no reader, but the import is module-level, so it is deploy-coupled too.

    The weekly object-store cron (`initial_setup/tasks/crons.yml`) runs exactly this on
    daniel-box, where the deploy lives beside claude-guard's. Pinned so the coupling is a
    known exit with the fix on stderr, never a repair that quietly did not run.
    """
    proc = run(
        [sys.executable, str(PRUNER), "--gc"],
        cwd=REPO,
        env=_without_deploy(tmp_path),
        check=False,
    )
    assert proc.returncode != 0
    assert proc.stdout == ""
    assert "chezmoi apply" in proc.stderr


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
