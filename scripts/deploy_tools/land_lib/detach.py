"""`land.sh --detach`: fork the landing into a logged child, and record where to find it.

WHAT THIS REPLACES. A session had to write three things around every landing, by hand, in the
exact shape the `land-after-merge` skill spelled out: `git rev-parse origin/master` for
`--since`, a redirect to a logfile under `$CLAUDE_JOB_DIR/tmp`, and `timeout 1200 tail -f -n +1
<log> | grep -m1 '^VERDICT:'` to block on the result. This module does the first two. The wait
is `cc-wait land <pr>`, whose source is `land_probe.py`: one wait loop for every repo, defined in
the dotfiles `cc-wait` package rather than once per waiter.

THE REDIRECT IS THE POINT, NOT AN ASIDE. Ansible refuses to start on a non-blocking stdout or
stderr ("Ansible requires blocking IO on stdin/stdout/stderr"), and a backgrounded Bash call
hands its child exactly that. `land.py:main` and `deploy_run.py` both clear O_NONBLOCK on their
own fds, but the flag lives on the open file description, so a handle that some other process
also holds can have it set again. A file opened here, by this process, cannot: that is why the
whole fix has always been "redirect to a file", and why doing it inside the script is safer
than asking a caller to remember.

WHAT IS SHARED WITH `deploy_detach.py`. The fork itself: `lib/detach_fork.py` owns the double
fork and the move out of a fan-out unit's cgroup, and both detach modes call it. The rest stays
here. `deploy_detach.run` takes the tree lock, makes a snapshot and hands its child lock
descriptors to keep, and its child posts a health verdict; a landing does none of that.

THE LANDING IS A GRANDCHILD, SO A KILL OF THE CALLER CANNOT REACH IT (issue #3158). A harness that
times out a Bash call kills the call's whole descendant tree, found by walking parent pids, not
just its process group. A child in its own session (`setsid`) is still a descendant, and on
2026-10-01 the landing of PR #3137 died that way 7m34s into a fleet deploy with no verdict line.
So `fork` forks twice through `detach_fork.fork_detached`: the landing reparents to init or the
nearest subreaper, outside the caller's tree, and it holds no inherited descriptor beyond its log
and /dev/null.

THE LANDING LEAVES A FAN-OUT UNIT'S CGROUP (issue #3160). A fan-out agent runs in
`fanout-<n>.service`, and stopping that unit kills its whole cgroup, grandchildren included. The
landing moves itself into `land<pr>-<pid>.scope` before it starts anything, so stopping the batch
leaves its landing running to its verdict. The log's first line names the scope to stop instead.

THE LANDING'S EXIT CODE IS THE AUTHORITY, NEVER THE GREP. A grandchild cannot be reaped by the
waiter, so the landing records its own exit code in `<log stem>.rc` after flushing its last line.
`land_probe.py` reads that file first and the log second. A landing can exit with no `VERDICT:`
line at all (`tests/test_land_broad_fallback_verdict.py` covers a truncated file list; a
landing-policy refusal leaves only its `land:` line, which `error_in` reads), and a
waiter that ended on the grep alone would either race the last flush or wait out its whole budget
on a run that had already finished. A landing that dies without writing the file -- SIGKILL, OOM
-- is reported as exactly that, through the pid `fork` records in `<log stem>.pid`.

THE LOG AND THE PID FILE EXIST BEFORE `land.sh --detach` RETURNS. The grandchild opens the log
itself, so without the parent's own `touch` a wait started straight after the return could find
no log, or an earlier landing's log for the same PR, and report that landing's verdict.
"""

import contextlib
import os
import pwd
import re
import sys
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # scripts/
from lib.detach_fork import close_inherited, fork_detached, leave_unit_cgroup

# Where a detached landing writes. `$CLAUDE_JOB_DIR/tmp` is what the skill told sessions to
# use, and it is per-session and cleaned up; this falls back to /tmp so the flag works from a
# plain shell and from a systemd unit. The fallback is one directory per user, suffixed with
# the user's name: the first landing creates it under its own umask, and the operator's 0007
# made a shared one 0770, which shut the `claude` agent user out.
LOG_DIR_ENV = "CLAUDE_JOB_DIR"
FALLBACK_LOG_DIR = Path("/tmp/homelab-landings")

_VERDICT_LINE = re.compile(r"^VERDICT:.*$", re.MULTILINE)
# `Landing.die` without a verdict (a landing-policy refusal) writes only this stderr message.
_ERROR_LINE = re.compile(r"^land: ", re.MULTILINE)
# A line that cannot belong to a `land:` message: the next phase header or a verdict.
_NOT_A_CONTINUATION = re.compile(r"^(== |VERDICT:)", re.MULTILINE)


def log_path(pr: str, log_dir: Path | None = None) -> Path:
    """Where this landing writes: `<dir>/land<pr>-<UTC stamp>.log`.

    The PR number leads, because that is what a session looking for its own log greps for, and
    `fanout_lib/status.py` already globs `land*.log`.
    """
    base = log_dir or default_log_dir()
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return base / f"land{pr}-{stamp}.log"


def default_log_dir() -> Path:
    """Where `land.sh --detach` writes when given no `--log-dir`. Creates nothing."""
    job_dir = os.environ.get(LOG_DIR_ENV)
    if job_dir:
        return Path(job_dir) / "tmp"
    user = pwd.getpwuid(os.geteuid()).pw_name
    return FALLBACK_LOG_DIR.with_name(f"{FALLBACK_LOG_DIR.name}-{user}")


def verdict_in(log: Path) -> str | None:
    """The landing's `VERDICT:` line from `log`, or None when it has not written one."""
    try:
        match = _VERDICT_LINE.search(log.read_text(errors="replace"))
    except OSError:
        return None
    return match.group(0) if match else None


def error_in(log: Path) -> str | None:
    """The landing's last `land: <why>` stderr message from `log`, or None when it has none.

    A stop that names no verdict leaves this as the only reason in the log, so a waiter
    reports it rather than a bare "no VERDICT line". The last one is the stop: earlier
    `land:` lines are warnings the landing carried on past.

    The message runs to the next phase header or verdict line, else to the end of the log,
    because a refusal can span lines: `merge._refuse_stray_closing_refs` lists the stray
    references and the remedy below its first line.
    """
    try:
        text = log.read_text(errors="replace")
    except OSError:
        return None
    starts = [m.start() for m in _ERROR_LINE.finditer(text)]
    if not starts:
        return None
    message = text[starts[-1] :]
    end = _NOT_A_CONTINUATION.search(message)
    return (message[: end.start()] if end else message).rstrip()


def rc_path(log: Path) -> Path:
    """Where the landing records its exit code: the log's own name with `.rc` for `.log`."""
    return log.with_suffix(".rc")


def pid_path(log: Path) -> Path:
    """Where `fork` records the landing's pid: the log's own name with `.pid` for `.log`."""
    return log.with_suffix(".pid")


def fork(log: Path, landing: Callable[[], int], scope_prefix: str = "land") -> int:
    """Run `landing` in a detached, logged grandchild; its pid, in the parent.

    The grandchild is in its own session, outside the caller's process tree, and out of any
    fan-out unit's cgroup. It gets stdin on /dev/null, stdout/stderr on `log` and no other
    inherited descriptor. It runs `landing`, writes the exit code to `rc_path(log)`, and leaves
    through `os._exit` so none of the parent's frames unwind twice. The parent creates `log`
    first and records the pid in `pid_path(log)` before it returns, so a wait started straight
    after the return finds this landing and no earlier one.

    Args:
      log: the file the landing's stdout and stderr are rebound to. Opened here, by this
        process, which is what guarantees the blocking handle Ansible needs.
      landing: what the grandchild runs; its return value is the recorded exit code.
      scope_prefix: the name of the transient scope the landing moves into, before its pid.

    Returns:
      The landing's pid. Only the parent ever returns from this function.
    """
    log.touch()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = fork_detached(lambda: _run_landing(log, landing, scope_prefix))
    pid_path(log).write_text(f"{pid}\n")
    return pid


def _run_landing(log: Path, landing: Callable[[], int], scope_prefix: str) -> None:
    """The grandchild's body: rebind stdio, drop inherited fds, leave the unit, run, record."""
    code = 1
    try:
        null = os.open(os.devnull, os.O_RDONLY)
        out = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(null, 0)
        os.dup2(out, 1)
        os.dup2(out, 2)
        close_inherited()
        # Before `landing` starts anything: a child started while the move is pending stays
        # in the unit's cgroup.
        moved = leave_unit_cgroup(scope_prefix)
        if moved:
            print(moved, flush=True)
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


def recorded_pid(log: Path) -> int | None:
    """The landing's pid as `fork` recorded it next to `log`, or None when there is none."""
    try:
        return int(pid_path(log).read_text().strip())
    except OSError, ValueError:
        return None


def alive(pid: int) -> bool:
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


def wait_command(pr: str, log: Path) -> str:
    """The `cc-wait` command that waits on this landing, and resumes the wait when re-run."""
    return f"cc-wait land {pr} --log {log}"


def announce(pid: int, log: Path, pr: str, out=None) -> None:
    """Tell the caller where the landing is and the one command that waits on it."""
    out = out if out is not None else sys.stdout
    print(f"land --detach: running in background (pid {pid}).", file=out)
    print(f"  log:  {log}", file=out)
    print(f"  wait: {wait_command(pr, log)}", file=out)
    out.flush()
