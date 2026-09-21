"""Tests for `_claude_worktree.py`, the bootstrap onto the deployed `claude_worktree` module.

`prune_worktrees.py` imports its readers through it (#2133). Two things have to hold: a
missing deploy raises, and removes nothing, rather than falling back to a stale copy; and
the copy CI runs against (`ansible/tests/_claude_worktree_stand_in/`) is the deployed
module byte for byte, which only a deployed host can check.

Run: uv run pytest scripts/dev/tests/test_claude_worktree_import.py
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DEV = Path(__file__).resolve().parents[1]
REPO = SCRIPTS_DEV.parents[1]
PRUNER = SCRIPTS_DEV / "prune_worktrees.py"
STAND_IN = (
    REPO / "ansible" / "tests" / "_claude_worktree_stand_in" / "claude_worktree.py"
)
# Wherever the bootstrap found it: the deploy, or what CLAUDE_WORKTREE_HOME named.
DEPLOYED = Path(sys.modules["claude_worktree"].__file__ or "")

# The plugin marks the module it substituted; the real deploy never carries the attribute.
_STAND_IN_FED = getattr(
    sys.modules.get("claude_worktree"), "__claude_worktree_stand_in__", False
)
_deployed_only = pytest.mark.skipif(
    _STAND_IN_FED,
    reason="the deployed claude_worktree module is not present, so the stand-in fed the "
    "suite and there is nothing deployed to diff it against",
)


def _without_deploy(tmp_path):
    """An environment in which no `claude_worktree` is reachable, in-process state aside."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["HOME"] = str(tmp_path)
    env["CLAUDE_WORKTREE_HOME"] = str(tmp_path / "absent")
    return env


def test_bootstrap_raises_when_the_deploy_is_missing(tmp_path):
    """No deploy means `_claude_worktree` raises and names the fix — not a stale fallback.

    A subprocess rather than an in-process import: the stand-in plugin has already fed this
    process a `claude_worktree`, and the env var is read once at import.
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
        env={**_without_deploy(tmp_path), "CLAUDE_WORKTREE_HOME": str(STAND_IN.parent)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(STAND_IN)


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


@_deployed_only
def test_the_ci_stand_in_is_the_deployed_module():
    """The stand-in is a second copy by construction; this is what diffs it.

    Skips where the deploy is absent (every CI run), since that is exactly where the
    stand-in is the only copy. Goes red on a deployed host the moment the dotfiles module
    changes and the stand-in does not — and `prek run` executes this suite before every
    commit from such a host. The fix is a copy, in either direction.
    """
    assert STAND_IN.read_bytes() == DEPLOYED.read_bytes(), (
        f"cp {DEPLOYED} {STAND_IN}  (or the reverse, if the stand-in is the newer one)"
    )


# The names `prune_worktrees.py` reads off `claude_worktree`. CI runs against the stand-in,
# so a name it lacks fails every test that imports the pruner at collection -- this pins
# the two together on the CI side, where the diff above skips.
_READERS_IMPORT = re.compile(r"^from claude_worktree import \(\n(.+?)\n\)", re.M | re.S)


def test_the_stand_in_carries_every_name_the_pruner_imports():
    match = _READERS_IMPORT.search(PRUNER.read_text())
    assert match, (
        "prune_worktrees.py no longer imports from claude_worktree in one block"
    )
    names = {n.strip().rstrip(",") for n in match.group(1).splitlines() if n.strip()}
    # Non-vacuity: the readers the pruner cannot work without.
    assert {"Worktree", "parse_worktree_list", "session_is_alive"} <= names
    stand_in_source = STAND_IN.read_text()
    missing = {
        n for n in names if not re.search(rf"^(def|class) {n}\b", stand_in_source, re.M)
    }
    assert not missing, f"stand-in lacks {sorted(missing)}"
