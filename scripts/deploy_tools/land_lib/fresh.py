"""`land.sh --fresh`: land from a detached checkout of origin/master made for this run (#7933).

`classify.refuse_stale_code` refuses a landing whose own code lags master. A checkout the caller
makes by hand goes stale again as soon as master moves, and the `git -C <primary> worktree add`
it took is a command a worktree-isolated session's guard refuses. `--fresh` makes that checkout
inside the landing, from the primary so it shares the primary's refs, runs the `land.sh` in it
with the same arguments minus `--fresh`, and removes it afterwards. The refusals name the flag.

The checkout's `land.sh` is a child process, not an exec, so this process outlives it and the
`finally` removes the checkout whatever the child's exit code. Under `--detach`, land.py forks
first and runs this inside the fork, so the checkout lives until the landing ends.
"""

import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # scripts/
from lib.git import git as _git

FRESH_FLAG = "--fresh"
BASE = "origin/master"
LAND_SH = "scripts/deploy_tools/land.sh"
# Each git step is a fetch or a local worktree change; ten minutes bounds a hung remote.
GIT_TIMEOUT_S = 600


def child_argv(argv: list[str], since: str) -> list[str]:
    """`argv` for the checkout's `land.sh`: without `--fresh` and `--detach`, with `--since`.

    `--detach` is dropped because the caller has already forked when it asked for it. `since`
    is added when the caller resolved it and `argv` names none, so the child keeps the
    pre-merge tip a detached landing reads before it arms.
    """
    out = [a for a in argv if a not in (FRESH_FLAG, "--detach")]
    if since and not any(a == "--since" or a.startswith("--since=") for a in out):
        out += ["--since", since]
    return out


def land_fresh(
    primary: Path,
    argv: list[str],
    git: Callable[..., subprocess.CompletedProcess[str]] = _git,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> int:
    """Run `land.sh argv` from a new detached worktree of the primary at origin/master.

    Args:
      primary: the checkout whose refs the worktree shares, and that fetches origin/master.
      argv: the arguments for the checkout's `land.sh`, already without `--fresh`.

    Returns:
      The child landing's exit code, or 1 when the checkout could not be made.
    """
    path = Path(tempfile.mkdtemp(prefix="land-fresh-"))
    try:
        for step in (
            ["fetch", "-q", "origin", "master"],
            ["worktree", "add", "-q", "--detach", str(path), BASE],
        ):
            why = _failed(git, primary, step)
            if why:
                print(
                    f"land: --fresh could not make a checkout of {BASE} "
                    f"(git {step[0]}: {why}); nothing was landed",
                    file=sys.stderr,
                )
                return 1
        print(
            f"land: landing from a fresh checkout of {BASE} at {path}", file=sys.stderr
        )
        return run([str(path / LAND_SH), *argv], check=False).returncode
    finally:
        git(
            "worktree",
            "remove",
            "--force",
            str(path),
            cwd=primary,
            check=False,
            timeout=120,
        )
        shutil.rmtree(path, ignore_errors=True)
        git("worktree", "prune", cwd=primary, check=False, timeout=120)


def _failed(
    git: Callable[..., subprocess.CompletedProcess[str]], cwd: Path, step: list[str]
) -> str:
    """Why `git step` failed in `cwd`, cut to its last 300 characters; "" when it succeeded."""
    try:
        done = git(*step, cwd=cwd, check=False, timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)[-300:]
    if done.returncode == 0:
        return ""
    return (
        " ".join((done.stdout + done.stderr).split())[-300:]
        or f"exit {done.returncode}"
    )
