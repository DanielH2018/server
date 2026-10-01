"""`land.sh --detach`: fork the landing into a logged child, and optionally wait for its verdict.

WHAT THIS REPLACES. A session had to write three things around every landing, by hand, in the
exact shape the `land-after-merge` skill spelled out: `git rev-parse origin/master` for
`--since`, a redirect to a logfile under `$CLAUDE_JOB_DIR/tmp`, and `timeout 1200 tail -f -n +1
<log> | grep -m1 '^VERDICT:'` to block on the result.

THE REDIRECT IS THE POINT, NOT AN ASIDE. Ansible refuses to start on a non-blocking stdout or
stderr ("Ansible requires blocking IO on stdin/stdout/stderr"), and a backgrounded Bash call
hands its child exactly that. `land.py:main` and `deploy_run.py` both clear O_NONBLOCK on their
own fds, but the flag lives on the open file description, so a handle that some other process
also holds can have it set again. A file opened here, by this process, cannot: that is why the
whole fix has always been "redirect to a file", and why doing it inside the script is safer
than asking a caller to remember.

WHY NOT `deploy_detach.py`. It cannot be reused: its `run` takes the tree lock, reaps snapshots,
makes one, takes per-service locks and hands the child a `locked.Run`, and its `child` posts a
health verdict through `deploy_detach_notify.py`. The genuinely shared part is four `dup2` calls.
Factoring those out would mean editing the deploy hot path for nothing, so the fork is written
here.

THE LANDING IS A GRANDCHILD, SO A KILL OF THE CALLER CANNOT REACH IT (issue #3158). A harness that
times out a Bash call kills the call's whole descendant tree, found by walking parent pids, not
just its process group. A child in its own session (`setsid`) is still a descendant, and on
2026-10-01 the landing of PR #3137 died that way 7m34s into a fleet deploy with no verdict line.
So `fork` forks twice: the intermediate child calls `setsid`, forks the landing and exits at once.
The landing reparents to init or the nearest subreaper, outside the caller's tree, and it holds
no inherited descriptor beyond its log and /dev/null.

THE LANDING'S EXIT CODE IS THE AUTHORITY, NEVER THE GREP. A grandchild cannot be reaped by the
waiter, so the landing records its own exit code in `<log stem>.rc` after flushing its last line.
`await_verdict` reads that file first and the log second. A landing can exit with no `VERDICT:`
line at all (`tests/test_land_broad_fallback_verdict.py` covers a truncated file list), and a
parent that exited on the grep alone would either race the last flush or wait out its whole budget
on a run that had already finished. A landing that dies without writing the file -- SIGKILL, OOM
-- is reported as exactly that.
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

# How long `--await-verdict` waits for the child, and how often it looks. The default is
# twenty minutes (the `timeout 1200` the skill's wait command carries).
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


def rc_path(log: Path) -> Path:
    """Where the landing records its exit code: the log's own name with `.rc` for `.log`."""
    return log.with_suffix(".rc")


def fork(log: Path, landing: Callable[[], int]) -> int:
    """Run `landing` in a detached, logged grandchild; its pid, in the parent.

    The intermediate child calls `setsid`, forks the landing and exits, so the landing is in its
    own session and outside the caller's process tree. The landing gets stdin on /dev/null,
    stdout/stderr on `log` and no other inherited descriptor. It runs `landing`, writes the exit
    code to `rc_path(log)`, and leaves through `os._exit` so none of the parent's frames unwind
    twice.

    Args:
      log: the file the landing's stdout and stderr are rebound to. Opened here, by this
        process, which is what guarantees the blocking handle Ansible needs.
      landing: what the grandchild runs; its return value is the recorded exit code.

    Returns:
      The landing's pid. Only the parent ever returns from this function.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid:
        os.close(write_end)
        with os.fdopen(read_end, "rb") as pipe:
            reported = pipe.read()
        os.waitpid(pid, 0)
        if not reported:
            raise RuntimeError("land --detach: the landing never started (fork failed)")
        return int(reported)
    try:
        os.close(read_end)
        os.setsid()
        grandchild = os.fork()
        if grandchild:
            os.write(write_end, str(grandchild).encode())
            os._exit(0)
        os.close(write_end)
        _run_landing(log, landing)
    finally:
        os._exit(1)


def _run_landing(log: Path, landing: Callable[[], int]) -> None:
    """The grandchild's body: rebind stdio, drop inherited fds, run, record the code, exit."""
    code = 1
    try:
        null = os.open(os.devnull, os.O_RDONLY)
        out = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(null, 0)
        os.dup2(out, 1)
        os.dup2(out, 2)
        # An inherited pipe of the caller's -- the harness's own output capture -- would keep
        # the caller's call open for as long as the landing runs.
        os.closerange(3, os.sysconf("SC_OPEN_MAX"))
        code = landing()
    except SystemExit as stop:
        code = stop.code if isinstance(stop.code, int) else 1
    except Exception:
        traceback.print_exc()
    finally:
        code = code if isinstance(code, int) else 1
        with contextlib.suppress(Exception):
            sys.stdout.flush()
            sys.stderr.flush()
        # After the flush, so a reader that sees the code also sees the last log line.
        with contextlib.suppress(Exception):
            rc = rc_path(log)
            tmp = rc.with_suffix(".rc.tmp")
            tmp.write_text(f"{code}\n")
            tmp.replace(rc)
        os._exit(code)


def recorded_code(log: Path) -> int | None:
    """The exit code the landing recorded next to `log`, or None when it has not written one."""
    try:
        return int(rc_path(log).read_text().strip())
    except OSError, ValueError:
        return None


def _alive(pid: int) -> bool:
    """Whether `pid` still runs. A zombie is dead: its new parent may never reap it."""
    with contextlib.suppress(ChildProcessError):
        # Our own child (only in tests): reap it, or it stays a zombie of this process.
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped:
            return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    # The state is the first field after the parenthesised command name.
    return stat.rpartition(")")[2].split()[0] not in ("Z", "X")


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
    """Wait for the landing, print its `VERDICT:` line, and return the code to exit with.

    Reads the recorded exit code first and `log` second: the code is the authority, and a
    landing can finish without writing a verdict at all. A landing that is gone without a
    recorded code was killed, and this says so. On timeout the landing is left running -- it
    holds the deploy locks and killing it mid-apply is worse than losing sight of it -- and
    this returns `LAND_GAVE_UP` with a line saying where to look.

    Returns:
      The landing's exit code, 1 when it died without recording one, or `LAND_GAVE_UP` when
      the budget elapsed first.
    """
    out = out if out is not None else sys.stdout
    deadline = clock() + timeout_s
    while True:
        code = recorded_code(log)
        # Checked again after the liveness probe: the landing writes its code, then exits.
        if code is None and not _alive(pid):
            code = recorded_code(log)
            if code is None:
                print(
                    f"land --detach: the landing (pid {pid}) died without recording an exit "
                    f"code; it was killed mid-run. Read {log}",
                    file=out,
                )
                code = 1
        if code is not None:
            verdict = verdict_in(log)
            print(verdict or f"VERDICT: (none printed — read {log})", file=out)
            out.flush()
            return code
        if clock() >= deadline:
            print(
                f"land --detach: no verdict within {timeout_s}s; the landing (pid {pid}) is "
                f"still running. Follow it with: tail -f {log}",
                file=out,
            )
            out.flush()
            return LAND_GAVE_UP
        sleep(poll_s)
