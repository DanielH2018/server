"""Which landing of a serial PR holds the serial lock, read without taking it.

`serial_landing` is the one read of that state. `cc-wait landing-clear` and `probe.py landing`
both call it, and `landing_blockers.blockers` words its answer for both.

`cc-wait landing-clear` runs outside the project venv, so this module imports the stdlib alone.
It owns the names that locate the landing locks (`TURN_HOST`, `LOCK_DIR`, `TURN_DIR_ENV`), which
`turn.py` and `slots.py` import from here.
"""

import inspect
import os
import shlex
import subprocess
from pathlib import Path

LOCK_DIR = Path("/run/lock")
# The host whose files are the landing locks. The lander units run there.
TURN_HOST = "daniel-box"
# Moves the locks into a directory on this host, so a test never takes the real ones.
TURN_DIR_ENV = "LAND_TURN_DIR"
# Held by a landing of a serial PR from the check before its merge until its process ends.
SERIAL_NAME = "homelab-land-serial.lock"
# A landing opens the lock file read-only (`turn._lock`), so the holder's number lives beside it.
HOLDER_NAME = "homelab-land-serial.holder"


def lock_name(pr: str) -> str:
    """The file name of the lock one landing of PR number `pr` holds (`pr_lock.py`)."""
    return f"homelab-land-pr-{pr}.lock"


def is_turn_host(hostname: str) -> bool:
    """Whether `hostname`, qualified or not, names the host that keeps the turn."""
    return hostname.split(".")[0] == TURN_HOST


def lock_held(path: str) -> bool:
    """Whether a process holds an exclusive flock on `path`, read from /proc/locks.

    It takes no lock itself, so it cannot make a landing's own attempt fail. It names
    nothing but `os`, because `_PROBE` runs this source on `TURN_HOST`.

    Raises:
        OSError: /proc/locks cannot be read, or `path` cannot be examined.
    """
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return False
    want = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    with open("/proc/locks") as locks:
        for line in locks:
            words = line.split()
            if "FLOCK" in words and "WRITE" in words and "->" not in words:
                if want in words:
                    return True
    return False


_PROBE = f"""import os, sys
{inspect.getsource(lock_held)}
print("held" if lock_held(sys.argv[1]) else "free")
"""


def probe_command(path: str) -> str:
    """The shell command that prints `held` or `free` for the lock file `path` on its host."""
    return shlex.join(["python3", "-c", _PROBE, path])


def note_holder(path: str, pr: str) -> None:
    """Write `pr` into the holder file `path`, replacing the number an earlier landing left.

    The file is mode 0666 so that a landing run by one user can replace the number another
    user's landing wrote. It names nothing but `os`, because `_NOTE` runs this source on
    `TURN_HOST`.

    Raises:
        OSError: The file can be neither opened nor created.
    """
    try:
        fd = os.open(path, os.O_WRONLY)
    except FileNotFoundError:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            os.fchmod(fd, 0o666)  # the umask would keep another user's number out
        except FileExistsError:
            fd = os.open(path, os.O_WRONLY)
    try:
        os.ftruncate(fd, 0)
        os.pwrite(fd, pr.encode(), 0)
    finally:
        os.close(fd)


def holder(lock: str, record: str, pr_lock: str) -> str | None:
    """The PR whose landing holds the serial lock `lock`, read from the holder file `record`.

    The number outlives the landing that wrote it, and a landing that could not write leaves
    an earlier landing's number in place. So the number counts only while that PR's own lock
    is held, which is `pr_lock` with `{pr}` replaced by the number. It names nothing but
    `lock_held`, because `_HOLDER` runs this source on `TURN_HOST`.

    Returns:
        None while the lock is free. Otherwise the PR number, or "" when the file names no PR
        whose landing is live.

    Raises:
        OSError: /proc/locks cannot be read, or a lock file cannot be examined.
    """
    if not lock_held(lock):
        return None
    try:
        with open(record) as fh:
            pr = fh.read(16).strip()
    except OSError:
        return ""
    if pr.isascii() and pr.isdigit() and lock_held(pr_lock.replace("{pr}", pr)):
        return pr
    return ""


_NOTE = f"""import os, sys
{inspect.getsource(note_holder)}
note_holder(sys.argv[1], sys.argv[2])
"""

_HOLDER = f"""import os, sys
{inspect.getsource(lock_held)}
{inspect.getsource(holder)}
pr = holder(*sys.argv[1:4])
print("free" if pr is None else "held " + pr)
"""


def _on_turn_host(script: str, args: list[str]) -> str:
    """Run `script` on `TURN_HOST` over ssh with `args`, and return what it printed.

    Raises:
        OSError: The host did not answer, or the script failed.
    """
    try:
        proc = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                TURN_HOST,
                shlex.join(["python3", "-c", script, *args]),
            ],  # fmt: skip
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise OSError(f"ssh {TURN_HOST}: timed out") from exc
    if proc.returncode != 0:
        raise OSError(f"ssh {TURN_HOST}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def _here(hostname: str) -> Path | None:
    """The directory of the landing locks when the host named `hostname` holds them."""
    moved = os.environ.get(TURN_DIR_ENV)
    if moved:
        return Path(moved)
    return LOCK_DIR if is_turn_host(hostname) else None


def record_holder(hostname: str, pr: str) -> None:
    """Name `pr` as the PR whose landing, run on `hostname`, holds the serial lock.

    Off the turn host this is one ssh call.

    Raises:
        OSError: The holder file could not be written, or the turn host did not answer.
    """
    here = _here(hostname)
    if here:
        note_holder(str(here / HOLDER_NAME), pr)
    else:
        _on_turn_host(_NOTE, [str(LOCK_DIR / HOLDER_NAME), pr])


def serial_landing(hostname: str) -> str | None:
    """The PR whose landing holds the serial lock, read from `hostname`.

    The read takes no lock, so it cannot make a landing's own attempt fail. Off the turn host
    it is one ssh call.

    Returns:
        None while no landing holds the lock. Otherwise the PR number, or "" for a landing
        that named no PR (`holder`).

    Raises:
        OSError: The host did not answer, or the answer was neither `held` nor `free`.
    """
    here = _here(hostname)
    directory = here or LOCK_DIR
    paths = [
        str(directory / SERIAL_NAME),
        str(directory / HOLDER_NAME),
        str(directory / lock_name("{pr}")),
    ]
    if here:
        return holder(*paths)
    answer = _on_turn_host(_HOLDER, paths)
    state, _, pr = answer.partition(" ")
    if state == "free" and not pr:
        return None
    if state == "held" and (not pr or (pr.isascii() and pr.isdigit())):
        return pr
    raise OSError(f"ssh {TURN_HOST}: answered {answer!r}")
