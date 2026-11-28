"""The PR lock: one live landing of a PR at a time, across both hosts and every user.

Two landings of one PR do the same work twice. The merge is pinned to a head, so the PR
merges once, but the second landing holds a place in the line and a landing slot for nothing.
So `land.py:main` takes this lock before it forks or merges, and a landing that finds it
held ends `refused` with exit 75 and names the landing that holds it.

The lock is an advisory lock on `homelab-land-pr-<n>.lock` on `TURN_HOST`, beside the turn
(`turn.py`). A landing there locks the file itself. A landing on another host holds the same
lock through an ssh session that lasts as long as the landing. Either way the lock ends with
the landing's process, so a landing that dies frees it, and a file left behind refuses
nothing. Nothing removes the files: /run/lock is a tmpfs.

sshd on `TURN_HOST` never probes a client, so a host that loses power or its network sends
no close. A landing on another host therefore writes a blank heartbeat line into the session
every `turn.HEARTBEAT_S`, as the turn does, and the script on `TURN_HOST` frees the lock once
three intervals pass without a line.

The holder writes one JSON line into the file: its host, pid, user and, when detached, its
log. A refused landing prints it. The file is mode 0666 so that a landing run by one user can
write its record into a file another user's landing created.

`--detach` takes the lock in the caller's process and the detached landing inherits it
(`Held.pass_on`), so the caller reads a refusal in the foreground. A fork inherits no thread,
so the detached landing starts its own heartbeat (`Held.for_fork`). A landing that runs
another `land.sh` for its own PR, from a fresh checkout (`fresh.py`), names the PR in
`HELD_ENV`, and that child takes no lock.

The lock is not a safety gate. A landing that cannot reach `TURN_HOST` or open the file goes
on without it. A lock held over ssh ends early when its ssh session does, such as when a
fan-out unit that started the landing is stopped, and the landing goes on.

`LAND_TURN_DIR` moves the lock with the turn, so a test never takes a real one.
"""

import fcntl
import inspect
import json
import os
import pwd
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from deploy_tools.land_lib import turn
from deploy_tools.land_lib.serial_lock import (
    LOCK_DIR,
    TURN_DIR_ENV,
    TURN_HOST,
    is_turn_host,
    lock_name,
)

# The PR whose lock an ancestor landing of this process holds.
HELD_ENV = "LAND_PR_LOCK_HELD"


def _lock(path: str) -> tuple[int | None, str]:
    """Open a PR's lock file and lock it without waiting.

    It names nothing but `os` and `fcntl`, because `_HOLD` runs this source on `TURN_HOST`.

    Returns:
        The locked descriptor and "", or None and the record of the landing that holds the
        lock.

    Raises:
        OSError: The file can be neither opened nor created.
    """
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o666)
            os.fchmod(fd, 0o666)  # the umask would keep another user's record out
        except FileExistsError:
            fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holder = os.pread(fd, 4096, 0).decode(errors="replace").strip()
        os.close(fd)
        return None, holder
    except OSError:
        os.close(fd)
        raise
    os.ftruncate(fd, 0)  # a dead landing's record
    return fd, ""


def _write(fd: int, record: str) -> None:
    """Replace the record in a locked PR lock file. `_HOLD` runs this source too."""
    os.ftruncate(fd, 0)
    os.pwrite(fd, record.strip().encode(), 0)


# argv: the heartbeat interval in seconds, then the lock file. A blank line is a heartbeat.
_HOLD = f"""import fcntl, os, select, sys
{inspect.getsource(_lock)}
{inspect.getsource(_write)}
fd, holder = _lock(sys.argv[2])
print("held" if fd is not None else "busy " + holder, flush=True)
rest = b""
while fd is not None and select.select([0], [], [], 3 * float(sys.argv[1]))[0]:
    got = os.read(0, 4096)
    if not got:
        break
    *records, rest = (rest + got).split(b"\\n")
    for record in records:
        if record.strip():
            _write(fd, record.decode(errors="replace"))
"""


@dataclass(frozen=True)
class Holder:
    """The landing that holds a PR's lock, as its record names it; "" for a field unread.

    Attributes:
        log: the log of a detached landing. A landing run without `--detach` has none.
    """

    host: str = ""
    pid: str = ""
    user: str = ""
    log: str = ""

    @classmethod
    def parse(cls, record: str) -> "Holder":
        """The holder a record names; an empty one for a record that does not parse."""
        try:
            fields = json.loads(record)
            return cls(*(str(fields.get(k) or "") for k in cls.__dataclass_fields__))
        except ValueError, AttributeError:
            return cls()

    def refusal(self, pr: str, hostname: str) -> str:
        """Why a second landing of `pr` did not start, and the command that waits instead."""
        if not self.pid:
            who = "its holder has written no record yet"
            wait = f"cc-wait land {pr}"
        else:
            who = f"host {self.host}, pid {self.pid}, user {self.user}"
            wait = f"cc-wait land {pr} --log {self.log}" if self.log else ""
            who += (
                f", log {self.log}"
                if self.log
                else ", no log: it runs without --detach"
            )
        text = (
            f"PR #{pr} — another landing of it is live ({who}); nothing is merged and no "
            "second landing started"
        )
        if not wait:
            return f"{text}. Re-run the same command once pid {self.pid} has ended"
        other = self.host and self.host.split(".")[0] != hostname.split(".")[0]
        return f"{text}. Wait on it with `{wait}`" + (
            f", run on {self.host}" if other else ""
        )


class Held:
    """A PR lock this landing holds until its process ends.

    Attributes:
        keep: the descriptors a fork must leave open to go on holding the lock.
    """

    def __init__(
        self,
        pr: str,
        hostname: str,
        keep: tuple[int, ...],
        write: Callable[[str], None],
        end: Callable[[], None],
        drop: Callable[[], None],
        beat: Callable[[], Callable[[], None]],
    ) -> None:
        self.keep = keep
        self._pr, self._hostname = pr, hostname
        self._write, self._end, self._drop, self._beat = write, end, drop, beat
        os.environ[HELD_ENV] = pr
        self._record(os.getpid(), None)
        self._stop_beat = beat()

    def _record(self, pid: int, log: Path | None) -> None:
        record = {
            "host": self._hostname,
            "pid": pid,
            "user": pwd.getpwuid(os.geteuid()).pw_name,
            "log": str(log or ""),
        }
        try:
            self._write(json.dumps(record))
        except OSError as exc:
            print(
                f"land: could not record who holds PR #{self._pr}'s lock ({exc})",
                file=sys.stderr,
            )

    def release(self) -> None:
        """Give the lock up. A landing calls it once, in the process that holds the lock."""
        os.environ.pop(HELD_ENV, None)
        self._stop_beat()
        self._end()

    def pass_on(self, pid: int, log: Path) -> None:
        """Leave the lock to the detached landing `pid`, forked from this process.

        The fork holds the lock through its own copies of `keep`, so this closes only this
        process's copies. `release` does nothing after it.
        """
        self._record(pid, log)
        os.environ.pop(HELD_ENV, None)
        self._drop()
        self._end = lambda: None

    def for_fork(self) -> Callable[[], None]:
        """The call a process forked from this one makes first, to go on holding the lock.

        Call it just before the fork. It stops this process's heartbeat, so the fork copies no
        thread. The fork inherits `keep` but no thread, so the call this returns starts the
        fork's own heartbeat.
        """
        self._stop_beat()

        def start() -> None:
            self._beat()

        return start


# The descriptors to keep, then the calls that write a record, end the lock, and drop this
# process's copies of it, and the call that starts a heartbeat in the calling process and
# returns the call that stops it.
_Taken = tuple[
    tuple[int, ...],
    Callable[[str], None],
    Callable[[], None],
    Callable[[], None],
    Callable[[], Callable[[], None]],
]


def _take_here(path: Path) -> _Taken | str:
    fd, holder = _lock(str(path))
    if fd is None:
        return holder

    def close() -> None:
        os.close(fd)

    return (fd,), lambda record: _write(fd, record), close, close, lambda: lambda: None


def _take_over_ssh(path: Path, beat: float) -> _Taken | str:
    answer, session, end = turn.hold_over_ssh(TURN_HOST, _HOLD, [str(beat), str(path)])
    if answer != "held":
        end()
        if answer.split(" ", 1)[0] == "busy":
            return answer.removeprefix("busy").strip()
        raise OSError(f"ssh {TURN_HOST}: answered {answer!r}")
    pipes = [p for p in (session.stdin, session.stdout, session.stderr) if p]
    stdin = pipes[0]

    def write(record: str) -> None:
        # One write on a pipe is atomic, so a heartbeat from another writer cannot split it.
        os.write(stdin.fileno(), (record + "\n").encode())

    def drop() -> None:
        for pipe in pipes:
            pipe.close()

    return (
        (stdin.fileno(),),
        write,
        end,
        drop,
        lambda: turn.start_heartbeat(stdin, beat),
    )


def take(
    pr: str,
    hostname: str,
    directory: Path = LOCK_DIR,
    beat: float = turn.HEARTBEAT_S,
) -> Held | Holder | None:
    """Take the lock of `pr` without waiting, for a landing on the host named `hostname`.

    Args:
        directory: where the lock files are on `TURN_HOST`; a test passes its own.
        beat: the seconds between heartbeats of a lock held over ssh; a test shortens it.

    Returns:
        The lock, or the landing that holds it. None when this landing goes on without the
        lock: an ancestor landing of this process holds it (`HELD_ENV`), or it cannot be
        opened.
    """
    if os.environ.get(HELD_ENV) == pr:
        return None
    moved = os.environ.get(TURN_DIR_ENV)
    path = (Path(moved) if moved else directory) / lock_name(pr)
    try:
        if moved or is_turn_host(hostname):
            got = _take_here(path)
        else:
            got = _take_over_ssh(path, beat)
    except (OSError, subprocess.SubprocessError) as exc:
        print(
            f"land: could not take PR #{pr}'s landing lock ({exc}); going on without it",
            file=sys.stderr,
        )
        return None
    if isinstance(got, str):
        return Holder.parse(got)
    return Held(pr, hostname, *got)
