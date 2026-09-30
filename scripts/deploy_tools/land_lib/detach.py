"""`land.sh --detach`: fork the landing into a logged child, and optionally wait for its verdict.

WHAT THIS REPLACES. A session had to write three things around every landing, by hand, in the
exact shape the `land-after-merge` skill spelled out: `git rev-parse origin/master` for
`--since`, a redirect to a logfile under `$CLAUDE_JOB_DIR/tmp`, and
`timeout 1200 tail -f -n +1 <log> | grep -m1 '^VERDICT:'` to block on the result. Each was a
step to get wrong, and the skill was 418 lines partly because it had to explain all three
(issue #2853).

THE REDIRECT IS THE POINT, NOT AN ASIDE. Ansible refuses to start on a non-blocking stdout or
stderr ("Ansible requires blocking IO on stdin/stdout/stderr"), and a backgrounded Bash call
hands its child exactly that. `land.py:main` and `deploy_run.py` both clear O_NONBLOCK on their
own fds, but the flag lives on the open file description, so a handle that some other process
also holds can have it set again. A file opened here, by this process, cannot: that is why the
whole fix has always been "redirect to a file", and why doing it inside the script is safer
than asking a caller to remember.

WHY NOT `deploy_detach.py`. Issue #2853 proposed reusing it. It does not survive contact: its
`run` takes the tree lock, reaps snapshots, makes one, takes per-service locks and hands the
child a `locked.Run`, and its `child` posts a health verdict through
`deploy_detach_notify.py`. The genuinely shared part is four `dup2` calls. Factoring those out
would mean editing the deploy hot path for nothing, so the fork is written here.

THE CHILD'S EXIT CODE IS THE AUTHORITY, NEVER THE GREP. `await_verdict` reaps the child first
and reads the log afterwards. A landing can exit with no `VERDICT:` line at all -- PR #2437 did,
on a truncated file list (`tests/test_land_broad_fallback_verdict.py`) -- and a parent that
exited on the grep alone would either race the child's last flush or wait out its whole budget
on a run that had already finished.
"""

import contextlib
import os
import re
import sys
import time
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # scripts/
from lib.exit_codes import LAND_GAVE_UP

# Where a detached landing writes. `$CLAUDE_JOB_DIR/tmp` is what the skill told sessions to
# use, and it is per-session and cleaned up; this falls back to /tmp so the flag works from a
# plain shell and from a systemd unit.
LOG_DIR_ENV = "CLAUDE_JOB_DIR"
FALLBACK_LOG_DIR = Path("/tmp/homelab-landings")

# How long `--await-verdict` waits for the child, and how often it looks. The default is the
# `timeout 1200` the skill's wait command carried, so a landing that used to be watched for
# twenty minutes still is.
AWAIT_TIMEOUT_S = 1200
AWAIT_POLL_S = 2.0

_VERDICT_LINE = re.compile(r"^VERDICT:.*$", re.MULTILINE)


def log_path(pr: str, log_dir: Path | None = None) -> Path:
    """Where this landing writes: `<dir>/land<pr>-<UTC stamp>.log`.

    The PR number leads, because that is what a session looking for its own log greps for, and
    `fanout_lib/status.py` already globs `land*.log`.
    """
    base = log_dir or _default_log_dir()
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return base / f"land{pr}-{stamp}.log"


def _default_log_dir() -> Path:
    job_dir = os.environ.get(LOG_DIR_ENV)
    return Path(job_dir) / "tmp" if job_dir else FALLBACK_LOG_DIR


def verdict_in(log: Path) -> str | None:
    """The landing's `VERDICT:` line from `log`, or None when it has not written one."""
    try:
        match = _VERDICT_LINE.search(log.read_text(errors="replace"))
    except OSError:
        return None
    return match.group(0) if match else None


def fork(log: Path, landing: Callable[[], int]) -> int:
    """Run `landing` in a forked, logged child; the child's pid, in the parent.

    The child gets its own session, stdin on /dev/null and stdout/stderr on `log`, then runs
    `landing` and leaves through `os._exit` so none of the parent's frames unwind twice.

    Args:
      log: the file the child's stdout and stderr are rebound to. Opened here, by this
        process, which is what guarantees the blocking handle Ansible needs.
      landing: what the child runs; its return value is the child's exit status.

    Returns:
      The child's pid. Only the parent ever returns from this function.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid:
        return pid
    code = 1
    try:
        os.setsid()
        null = os.open(os.devnull, os.O_RDONLY)
        out = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(null, 0)
        os.dup2(out, 1)
        os.dup2(out, 2)
        os.close(null)
        os.close(out)
        code = landing()
    except SystemExit as stop:
        code = stop.code if isinstance(stop.code, int) else 1
    except Exception:
        traceback.print_exc()
    finally:
        with contextlib.suppress(Exception):
            sys.stdout.flush()
            sys.stderr.flush()
        os._exit(code if isinstance(code, int) else 1)


def announce(pid: int, log: Path, awaiting: bool, out=None) -> None:
    """Tell the caller where the landing is and how to follow it."""
    out = out if out is not None else sys.stdout
    print(f"land --detach: running in background (pid {pid}).", file=out)
    print(f"  log:  {log}", file=out)
    print(f"  tail: tail -f {log}", file=out)
    if awaiting:
        print(
            f"  Waiting up to {AWAIT_TIMEOUT_S}s for this landing's own VERDICT line.",
            file=out,
        )
    out.flush()


def await_verdict(
    pid: int,
    log: Path,
    timeout_s: int = AWAIT_TIMEOUT_S,
    poll_s: float = AWAIT_POLL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    out=None,
) -> int:
    """Wait for the child, print its `VERDICT:` line, and return the code to exit with.

    Reaps `pid` first and reads `log` second: the exit code is the authority, and a landing
    can finish without writing a verdict at all. On timeout the child is left running -- it
    holds the deploy locks and killing it mid-apply is worse than losing sight of it -- and
    this returns `LAND_GAVE_UP` with a line saying where to look.

    Returns:
      The child's exit status, or `LAND_GAVE_UP` when the budget elapsed first.
    """
    out = out if out is not None else sys.stdout
    deadline = clock() + timeout_s
    while True:
        reaped, status = os.waitpid(pid, os.WNOHANG)
        if reaped:
            verdict = verdict_in(log)
            print(verdict or f"VERDICT: (none printed — read {log})", file=out)
            out.flush()
            return os.waitstatus_to_exitcode(status)
        if clock() >= deadline:
            print(
                f"land --detach: no verdict within {timeout_s}s; the landing (pid {pid}) is "
                f"still running. Follow it with: tail -f {log}",
                file=out,
            )
            out.flush()
            return LAND_GAVE_UP
        sleep(poll_s)
