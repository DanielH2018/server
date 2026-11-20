"""Whether a landing of a serial PR holds the serial lock, read without taking it.

`cc-wait landing-clear` runs outside the project venv, so this module imports the stdlib alone.
It owns the names that locate the landing locks (`TURN_HOST`, `LOCK_DIR`, `TURN_DIR_ENV`), which
`turn.py` and `slots.py` import from here.
"""

import inspect
import os
import shlex
import subprocess
from pathlib import Path

# /run/lock is a tmpfs every user can create a file in, and one file there serves every
# landing on the host, whichever user runs it.
LOCK_DIR = Path("/run/lock")
# The host whose files are the landing locks. The lander units run there.
TURN_HOST = "daniel-box"
# Moves the locks into a directory on this host, so a test never takes the real ones.
TURN_DIR_ENV = "LAND_TURN_DIR"
# Held by a landing of a serial PR from the check before its merge until its process ends.
SERIAL_NAME = "homelab-land-serial.lock"


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


def serial_landing_live(hostname: str) -> bool:
    """Whether a landing of a serial PR holds the serial lock, read from `hostname`.

    The read takes no lock, so it cannot make a landing's own attempt fail.

    Raises:
        OSError: The host did not answer, or the answer was neither `held` nor `free`.
    """
    moved = os.environ.get(TURN_DIR_ENV)
    if moved:
        return lock_held(str(Path(moved) / SERIAL_NAME))
    path = str(LOCK_DIR / SERIAL_NAME)
    if is_turn_host(hostname):
        return lock_held(path)
    try:
        proc = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                TURN_HOST,
                probe_command(path),
            ],  # fmt: skip
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise OSError(f"ssh {TURN_HOST}: timed out") from exc
    if proc.returncode != 0 or proc.stdout.strip() not in ("held", "free"):
        raise OSError(f"ssh {TURN_HOST}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip() == "held"
