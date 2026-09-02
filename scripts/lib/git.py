"""One way to run git from a script, with the repository chosen by ``cwd`` alone.

WHY. Five scripts each carried a private ``_git()`` by 2026-09-01, and they disagreed on
the two things that matter: whether a non-zero exit raises, and whether an inherited
``GIT_DIR`` could redirect the call at another repository. The second one is not
hypothetical -- inside a git hook ``GIT_DIR`` and ``GIT_WORK_TREE`` are both set and point
at the repo running the hook, and ``git -C`` does not override them, so ``head_sha()`` once
reported the hook's repo for every path it was handed.

Every ``GIT_*`` variable is stripped before the call, so ``cwd`` is the only thing that
decides which tree is read. This is for reads and worktree bookkeeping: a script that
commits and needs ``GIT_AUTHOR_*`` passes its own ``env`` to ``subprocess`` directly.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def git(
    *args: str,
    cwd: str | Path | None = None,
    check: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` in ``cwd`` and return the completed process.

    ``check=True`` raises ``CalledProcessError`` on a non-zero exit, the way most callers
    want a failed ``rev-parse`` to surface. Pass ``check=False`` to read ``returncode``
    yourself, as a ``merge-base --is-ancestor`` test does.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def git_stdout(*args: str, cwd: str | Path | None = None, **kwargs) -> str:
    """``git(...).stdout`` with surrounding whitespace removed."""
    return git(*args, cwd=cwd, **kwargs).stdout.strip()
