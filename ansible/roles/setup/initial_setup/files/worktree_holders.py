#!/usr/bin/python3 -I
"""List every process that holds a path under the sudo caller's Claude worktrees, as root.

The weekly worktree sweep runs as the checkout owner, and `/proc/<pid>/cwd` and `environ`
refuse that uid for another user's process. `lib.worktrees.processes_using` used to see
another uid only inside the caller's own login slice (#3994), so a `claude` process started
through `su`, `runuser -l` or `systemd-run --uid` could hold a worktree unseen (#4170).

Installed root-owned at /usr/local/libexec/worktree-holders and run through a NOPASSWD
sudoers rule that allows it with NO arguments, so the caller cannot point it at another
path. The root it reports under is derived from `SUDO_UID`: that user's
`~/server/.claude/worktrees`. One line per holder, tab-separated:

    <pid>\tcwd\t<path>
    <pid>\tCLAUDE_PROJECT_DIR\t<path>
    <pid>\tunreadable\t<error>

An `unreadable` line is a process root itself could not read. The caller counts it as a
holder of every tree, because it could be any of them. Nothing else from `environ` is ever
printed: a Claude process carries tokens there, and the sweep's output goes to the journal.

`#!/usr/bin/python3 -I`: the distro interpreter in isolated mode, so no `PYTHON*` variable
and no user site directory reaches a root process.
"""

import errno
import os
import pwd
import sys
from pathlib import Path

# The errors a process that exited mid-scan raises. Matched by errno rather than by exception
# class: the repo's formatter rewrites a parenthesised `except (A, B):` into the 3.14-only
# bare form, and this file runs on the distro interpreter.
_GONE = (errno.ENOENT, errno.ESRCH)


def _inside(held: str, root: Path) -> bool:
    path = Path(held).resolve()
    return path == root or root in path.parents


def scan(root: Path, proc: Path = Path("/proc")) -> list[tuple[int, str, str]]:
    """(pid, kind, value) for every process whose cwd or `CLAUDE_PROJECT_DIR` is under `root`.

    A process that exits mid-scan raises ENOENT or ESRCH and is skipped. Any other read
    error is reported as `unreadable`, so the caller fails closed.
    """
    root = root.resolve()
    found = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            cwd = os.readlink(entry / "cwd")
        except OSError as e:
            if e.errno not in _GONE:
                found.append((pid, "unreadable", f"cwd: {e.strerror}"))
                continue
            # Gone, or a zombie, which has no cwd. Its environ answers the same way.
            cwd = ""
        # A cwd whose directory was deleted reads back as "<path> (deleted)", which never
        # resolves under an existing root.
        if cwd and _inside(cwd, root):
            found.append((pid, "cwd", cwd))
            continue
        try:
            environ = (entry / "environ").read_bytes().split(b"\0")
        except OSError as e:
            if e.errno not in _GONE:
                found.append((pid, "unreadable", f"environ: {e.strerror}"))
            continue
        for var in environ:
            if var.startswith(b"CLAUDE_PROJECT_DIR="):
                held = var.partition(b"=")[2].decode(errors="replace")
                if held and _inside(held, root):
                    found.append((pid, "CLAUDE_PROJECT_DIR", held))
                break
    return found


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        print("worktree-holders takes no arguments", file=sys.stderr)
        return 2
    sudo_uid = os.environ.get("SUDO_UID", "")
    if not sudo_uid.isdigit():
        print("worktree-holders runs only through sudo (no SUDO_UID)", file=sys.stderr)
        return 2
    root = Path(pwd.getpwuid(int(sudo_uid)).pw_dir) / "server" / ".claude" / "worktrees"
    for pid, kind, value in scan(root):
        print(f"{pid}\t{kind}\t{value}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
