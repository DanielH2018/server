"""`land.sh --fresh`: land from a detached checkout of origin/master made for this run.

A checkout the caller makes by hand goes stale again as soon as master moves, and the
`git -C <primary> worktree add` it took is a command a worktree-isolated session's guard
refuses. `--fresh` makes that checkout inside the landing, from the primary so it shares the
primary's refs, runs the `land.sh` in it with the same arguments minus `--fresh`, and removes
it afterwards.

A landing whose own code lags master reaches the same checkout without the flag. Before the
merge, `classify.refuse_stale_code_before_merge` raises `LandFresh`, and land.py re-runs the
whole landing there. After the merge, `classify.refuse_stale_code` raises `GradeFresh`, and
land.py runs the merged PR's grading there.

The landing `land_fresh` runs reads `CHILD_ENV` and never re-runs itself that way, so a master
that moves again ends that landing `refused` and no landing re-runs twice.

A landing that holds the landing turn with landing code older than master's hands the turn to
the same kind of checkout (`turn.HandOver`), so the turn's holder forms its batch with
master's code. The child inherits the turn's locked descriptor, named in `TURN_FD_ENV`.

The checkout's `land.sh` is a child process, not an exec, so this process outlives it and the
`finally` removes the checkout whatever the child's exit code. Under `--detach`, land.py forks
first and runs this inside the fork, so the checkout lives until the landing ends.

The checkout lives under `checkouts_root()`, on disk: `/tmp` on daniel-box is a tmpfs, where
each 353 MB checkout held RAM. SIGTERM unwinds through the `finally` too. This process first
passes the signal to the child landing and waits for it to end, so the child neither keeps the
landing turn nor runs on in a removed checkout. A landing killed outright leaves its checkout
behind, so each checkout carries an `flock` that this process and the child landing hold, and
every fresh landing first removes the checkouts no process holds.
"""

import contextlib
import fcntl
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable, Generator
from pathlib import Path

from deploy_tools.land_lib.options import Options
from lib.git import git as _git

FRESH_FLAG = "--fresh"
BASE = "origin/master"
LAND_SH = "scripts/deploy_tools/land.sh"
# The landing turn's locked descriptor, which a child landing inherits (`turn._adopt`).
TURN_FD_ENV = "LAND_TURN_FD"
# Set for the landing `land_fresh` runs, which refuses where its parent re-ran.
CHILD_ENV = "LAND_FRESH_CHILD"
# Each git step is a fetch or a local worktree change; ten minutes bounds a hung remote.
GIT_TIMEOUT_S = 600
# How long a stopped landing waits for its child to record its stop before killing it.
STOP_WAIT_S = 60
CHECKOUT_PREFIX = "land-fresh-"


# Where a landing's worktrees go: on disk, because `/tmp` on daniel-box is a tmpfs.
LANDING_CACHE = "~/.cache/homelab"


def checkouts_root() -> Path:
    """The directory the fresh checkouts go in, under the landing user's `LANDING_CACHE`."""
    return Path(LANDING_CACHE).expanduser() / "land-fresh"


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


class GradeFresh(Exception):
    """A merged PR whose grading moves to a fresh checkout, because this one lags master.

    Attributes:
      argv: the arguments for the fresh checkout's `land.sh`.
    """

    def __init__(self, argv: list[str], why: str) -> None:
        super().__init__(why)
        self.argv = argv


class LandFresh(Exception):
    """An unmerged PR whose landing re-runs from a fresh checkout, because this one lags master."""


class CheckoutFailed(Exception):
    """The fresh checkout's `land.sh` never started: the checkout or the exec failed."""


class Stopped(KeyboardInterrupt):
    """A SIGTERM or SIGINT that `dies_on_a_signal` turned into an unwind.

    Attributes:
      signum: the signal's number.
    """

    def __init__(self, signum: int) -> None:
        super().__init__(f"signal {signum}")
        self.signum = signum


def grading_argv(opts: Options, since: str) -> list[str]:
    """`argv` that grades the merged PR `opts` names, with no flag that merges or forks.

    Without `--await-merge`, the child's checkout is made after the merge, so it holds the
    merge commit's parent and cannot find its own code stale again.
    """
    argv = ["--pr", opts.pr, "--since", since, "--ci-timeout", str(opts.ci_timeout)]
    if opts.tags:
        argv += ["--tags", opts.tags]
    if opts.verdict_file:
        argv += ["--verdict-file", opts.verdict_file]
    return argv


def land_fresh(
    primary: Path,
    argv: list[str],
    turn_fd: int | None = None,
    git: Callable[..., subprocess.CompletedProcess[str]] = _git,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> int:
    """Run `land.sh argv` from a new detached worktree of the primary at origin/master.

    Args:
      primary: the checkout whose refs the worktree shares, and that fetches origin/master.
      argv: the arguments for the checkout's `land.sh`, already without `--fresh`.
      turn_fd: the landing turn's locked descriptor, which the child landing inherits; the
        caller closes its own copy once this returns.

    Returns:
      The child landing's exit code.

    Raises:
      CheckoutFailed: no child landing started, so nothing printed or recorded a verdict.
      Stopped: a SIGTERM or SIGINT arrived. The child got the same signal and has ended, or
        was killed after `STOP_WAIT_S`, before the checkout is removed.
    """
    root = checkouts_root()
    root.mkdir(parents=True, exist_ok=True)
    sweep_dead_checkouts(primary, root, git)
    path = Path(tempfile.mkdtemp(prefix=CHECKOUT_PREFIX, dir=root))
    lock_fd = _hold(path)
    try:
        for step in (
            ["fetch", "-q", "origin", "master"],
            ["worktree", "add", "-q", "--detach", str(path), BASE],
        ):
            why = _failed(git, primary, step)
            if why:
                raise CheckoutFailed(
                    f"--fresh could not make a checkout of {BASE} (git {step[0]}: {why})"
                )
        print(
            f"land: landing from a fresh checkout of {BASE} at {path}", file=sys.stderr
        )
        inherit: dict = {
            "env": {**os.environ, CHILD_ENV: "1"},
            "pass_fds": (lock_fd,),
        }
        if turn_fd is not None:
            inherit["pass_fds"] = (lock_fd, turn_fd)
            inherit["env"][TURN_FD_ENV] = str(turn_fd)
        with dies_on_a_signal():
            try:
                child = popen([str(path / LAND_SH), *argv], **inherit)
            except OSError as exc:
                raise CheckoutFailed(
                    f"--fresh could not start {LAND_SH} in its checkout ({exc})"
                ) from exc
            try:
                return child.wait()
            except Stopped as stop:
                _stop_child(child, stop.signum)
                raise
    finally:
        _remove(primary, path, git)
        os.close(lock_fd)


def _stop_child(child: subprocess.Popen, signum: int) -> None:
    """Pass `signum` to `child` and wait for it to unwind; kill it after `STOP_WAIT_S`.

    The child is `land.sh`, which execs `uv run`, and `uv run` passes SIGTERM on to the
    landing. A SIGKILL here would instead orphan that landing with the turn's descriptor.
    """
    child.send_signal(signum)
    try:
        child.wait(timeout=STOP_WAIT_S)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def sweep_dead_checkouts(
    primary: Path,
    root: Path,
    git: Callable[..., subprocess.CompletedProcess[str]] = _git,
) -> list[Path]:
    """Remove each checkout under `root` whose lock no process holds, and return them.

    A landing killed by SIGKILL, or a reboot, skips `land_fresh`'s `finally`. Its lock dies
    with the last process holding it, so a free lock means no landing uses the checkout.
    """
    swept = []
    for path in sorted(root.glob(CHECKOUT_PREFIX + "*")):
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            continue
        finally:
            os.close(fd)
        _remove(primary, path, git)
        swept.append(path)
    if swept:
        print(
            f"land: removed {len(swept)} fresh checkout(s) a killed landing left in {root}",
            file=sys.stderr,
        )
    return swept


def _hold(path: Path) -> int:
    """An inheritable descriptor holding `path`'s exclusive lock, for the landing's lifetime."""
    fd = os.open(path, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.set_inheritable(fd, True)
    return fd


def _remove(
    primary: Path, path: Path, git: Callable[..., subprocess.CompletedProcess[str]]
) -> None:
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


@contextlib.contextmanager
def dies_on_a_signal() -> Generator[None]:
    """SIGTERM and SIGINT raise inside the block, so an enclosing `finally` still runs.

    Default SIGTERM disposition terminates the interpreter outright, skipping every `finally`
    -- which for `land_fresh` and `tools.gate_snapshot` leaves a worktree registered in the
    primary that nothing collects. It raises `Stopped`, a `KeyboardInterrupt`: `land.py`
    catches no `KeyboardInterrupt` (its own `finally` suppresses `Exception` only, so this
    propagates through it after the ledger row is written), and a SIGTERM'd landing and a
    Ctrl-C'd one stay on one path. The previous handlers are restored on the way out, because
    the test suite runs this in pytest's main thread.
    """

    def raise_it(signum, _frame):
        raise Stopped(signum)

    previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    for sig in previous:
        signal.signal(sig, raise_it)
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


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
