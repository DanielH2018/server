"""Tests for `_claude_worktree.py`, the bootstrap onto the deployed `claude_worktree` module.

`prune_worktrees.py` imports its readers through it (#2133). What has to hold: a missing
deploy raises, and removes nothing, rather than falling back to a stale copy; and the env
var names the copy that gets imported. CI links a pinned dotfiles checkout into the deployed
path (#2812), so every test here runs there too.

Run: uv run pytest scripts/dev/tests/test_claude_worktree_import.py
"""

import os
import subprocess
import sys
from pathlib import Path

# The bootstrap, imported rather than read out of sys.modules, so a host or runner with no
# deploy fails collection here with the bootstrap's own message.
import _claude_worktree  # noqa: F401
import claude_worktree

SCRIPTS_DEV = Path(__file__).resolve().parents[1]
REPO = SCRIPTS_DEV.parents[1]
PRUNER = SCRIPTS_DEV / "prune_worktrees.py"
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
    proc = subprocess.run(
        [sys.executable, "-c", "import _claude_worktree"],
        cwd=SCRIPTS_DEV,
        env=_without_deploy(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "ImportError" in proc.stderr
    assert str(tmp_path / "absent") in proc.stderr
    assert "chezmoi apply" in proc.stderr


def test_bootstrap_imports_the_module_the_env_var_names(tmp_path):
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import _claude_worktree, claude_worktree; print(claude_worktree.__file__)",
        ],
        cwd=SCRIPTS_DEV,
        env={**_without_deploy(tmp_path), "CLAUDE_WORKTREE_HOME": str(DEPLOYED.parent)},
        capture_output=True,
        text=True,
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
    proc = subprocess.run(
        [sys.executable, str(PRUNER), "--prune"],
        cwd=REPO,
        env=_without_deploy(tmp_path),
        capture_output=True,
        text=True,
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
    proc = subprocess.run(
        [sys.executable, str(PRUNER), "--gc"],
        cwd=REPO,
        env=_without_deploy(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert proc.stdout == ""
    assert "chezmoi apply" in proc.stderr


def test_the_pruner_imports_as_a_library_with_only_scripts_on_the_path(tmp_path):
    """`backlog.py` and `findings_lib` load the pruner as `dev.prune_worktrees`.

    That puts `scripts/` on `sys.path` but not `scripts/dev/`, so a bare sibling import that
    works when the pruner runs directly raises here instead. PR #2578's
    `from foreign_owned import` did exactly that, and the docs-refresh cron's `backlog.py`
    generator failed on every run until it was spelled `dev.foreign_owned` (2026-09-25).
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["CLAUDE_WORKTREE_HOME"] = str(DEPLOYED.parent)
    code = (
        f"import sys; sys.path.insert(0, {str(SCRIPTS_DEV.parent)!r}); "
        "import dev.prune_worktrees"
    )
    run = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr
