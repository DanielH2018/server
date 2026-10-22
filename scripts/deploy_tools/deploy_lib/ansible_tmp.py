"""The temp directories ansible-core leaves under `~/.ansible/tmp`: this run's, and stale ones.

ansible-core makes two kinds there. Importing its constants creates `ansible-local-<pid><8
random chars>` and registers only an atexit handler to remove it, so any exit that skips atexit
leaves it behind: an `os.execvp` (#4893), a SIGKILL, an `os._exit`. Running a module on a
`connection=local` host creates `ansible-tmp-<time>-<pid>-<rand>` holding the module's
`AnsiballZ_*.py`, removed when the task ends, so a controller killed mid-task leaves that one
(#5000). Nothing else ever removes either, and daniel-box had 2764 and 34 of them on 2026-10-22.

`sweep_stale` removes what a dead run left, on every deploy.sh run that reaches the deploy.
It logs each removal through `logger -t SYSLOG_IDENT`, so a leak that keeps happening stays
countable in the journal after the sweep has cleared it:
`journalctl -t deploy-ansible-tmp-sweep`.
"""

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SYSLOG_IDENT = "deploy-ansible-tmp-sweep"

# A day. A deploy's module runs for minutes, and a full deploy takes about 20, so a directory
# this old belongs to a run that is over.
STALE_AFTER_S = 24 * 60 * 60

# `tempfile.mkdtemp(prefix="ansible-local-%s" % os.getpid())` appends 8 random characters.
_LOCAL = re.compile(r"ansible-local-(\d+)[a-z0-9_]{8}")
_MODULE = re.compile(r"ansible-tmp-[0-9.]+-\d+-\d+")


def drop_ansible_local_tmp() -> None:
    """Remove the local temp directory ansible-core made for this process, if it made one.

    Read from `sys.modules` rather than imported: importing the constants is what creates the
    directory, so a run that never loaded ansible must not load it here.
    """
    path = _own_local_tmp()
    if path is not None:
        shutil.rmtree(path, ignore_errors=True)


def _own_local_tmp() -> str | None:
    constants = sys.modules.get("ansible.constants")
    path = getattr(constants, "DEFAULT_LOCAL_TMP", None)
    return path if isinstance(path, str) else None


def tmp_dirs() -> list[Path]:
    """The directories ansible-core writes the two kinds into, each named once.

    The local one is `ANSIBLE_LOCAL_TEMP` when it is set, else the parent of this process's
    own `ansible-local-*` when ansible is loaded, which is every run past the flag parsing.
    The variable goes first because the constant is fixed at import: a process that imported
    ansible before the variable changed, such as a test worker, still holds the old parent.
    The remote one is the shell plugin's `remote_tmp`, which `ansible.cfg` leaves at its
    default.
    """
    own = _own_local_tmp()
    if "ANSIBLE_LOCAL_TEMP" in os.environ:
        local = Path(os.environ["ANSIBLE_LOCAL_TEMP"]).expanduser()
    elif own:
        local = Path(own).parent
    else:
        local = Path("~/.ansible/tmp").expanduser()
    remote = Path(
        os.environ.get("ANSIBLE_REMOTE_TEMP")
        or os.environ.get("ANSIBLE_REMOTE_TMP")
        or "~/.ansible/tmp"
    ).expanduser()
    return list(dict.fromkeys([local, remote]))


def _alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def is_stale(entry: Path, now: float) -> bool:
    """True when `entry` is a leftover of a run that has ended.

    An `ansible-local-*` must also be empty and its process gone: a long-lived process, such
    as a service that imported ansible days ago, still owns its directory. An `ansible-tmp-*`'s
    pid is a forked worker's, which says nothing about the play, so age alone decides it.
    """
    try:
        old = now - entry.lstat().st_mtime > STALE_AFTER_S
        if not old or not entry.is_dir() or entry.is_symlink():
            return False
        local = _LOCAL.fullmatch(entry.name)
        if local:
            return not any(entry.iterdir()) and not _alive(int(local.group(1)))
        return _MODULE.fullmatch(entry.name) is not None
    except OSError:
        return False


def sweep_stale(dirs: list[Path] | None = None, now: float | None = None) -> int:
    """Remove every stale ansible temp directory in `dirs`; returns how many it removed.

    `dirs` defaults to `tmp_dirs()`, and `now` to the clock.

    Never raises: a deploy must not fail over housekeeping.
    """
    now = time.time() if now is None else now
    removed: list[str] = []
    for parent in tmp_dirs() if dirs is None else dirs:
        try:
            entries = sorted(parent.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not is_stale(entry, now):
                continue
            try:
                stamp = time.gmtime(entry.lstat().st_mtime)
            except OSError:  # another process removed it first
                continue
            mtime = time.strftime("%Y-%m-%dT%H:%M:%SZ", stamp)
            shutil.rmtree(entry, ignore_errors=True)
            if not entry.exists():
                removed.append(f"removed {entry} (mtime {mtime})")
    if removed:
        _log(removed)
    return len(removed)


def _log(lines: list[str]) -> None:
    """One `logger` call for the whole batch: the first sweep on a host removes thousands."""
    try:
        subprocess.run(
            ["logger", "-t", SYSLOG_IDENT],
            input="\n".join(lines) + "\n",
            text=True,
            check=False,
            timeout=30,
        )
    except OSError, subprocess.TimeoutExpired:
        pass
