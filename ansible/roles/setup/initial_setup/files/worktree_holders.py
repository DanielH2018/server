#!/usr/bin/python3 -I
"""List every process that holds a path under the caller's Claude worktrees, as root.

The weekly worktree sweep runs as the checkout owner, and `/proc/<pid>/cwd` and `environ`
refuse that uid for another user's process. `lib.worktrees.processes_using` used to see
another uid only inside the caller's own login slice (#3994), so a `claude` process started
through `su`, `runuser -l` or `systemd-run --uid` could hold a worktree unseen (#4170).

Installed root-owned at /usr/local/libexec/worktree-holders and run by
`worktree-holders@.service`, one instance per connection to `worktree-holders.socket`
(`Accept=yes`), with the connection on stdin. A socket needs no setuid, so a caller with
`NoNewPrivileges=yes`, such as a claude-rc.service session, can ask it too (#4297). The
socket's group decides who may connect. The kernel names the caller through `SO_PEERCRED`,
and the root this reports under is that uid's user's entry in /etc/worktree-holders.json.
initial_setup renders that map from the same list it puts in the socket's group: the
operator's checkout, and each present agent user's own clone (#4295). So a `claude` removal
in /var/lib/claude/server sees an `ubuntu` process there (#4021). A uid with no entry is
refused. The caller sends nothing; it cannot point the scan at another path.

The answer is a header line, one line per holder, and a terminator:

    ok
    <pid>\tcwd\t<path>
    <pid>\tCLAUDE_PROJECT_DIR\t<path>
    <pid>\tunreadable\t<error>
    end

or a single `refused\t<reason>` line. A socket carries no exit status, so the caller treats
an answer without the header or the terminator as a refusal: an instance that died mid-scan
must not read as "no holders".

Every value is escaped with Python's `unicode_escape` codec, so a line holds only printable
ASCII with no tab. A cwd whose directory name holds a line break (`\\n`, U+2028 and the rest
`str.splitlines()` breaks on) or a tab therefore stays one line, and the caller decodes it
back to the real path (#4294). The cwd cannot be dropped the way an unprintable value
once was (#4272), because the cwd is the hold.

An `unreadable` line is a process root itself could not read. The caller counts it as a
holder of every tree, because it could be any of them. Nothing else from `environ` is ever
printed: a Claude process carries tokens there, and the sweep's output goes to the journal.

`#!/usr/bin/python3 -I`: the distro interpreter in isolated mode, so no `PYTHON*` variable
and no user site directory reaches a root process.
"""

import errno
import json
import os
import pwd
import socket
import struct
import sys
from pathlib import Path

# The errors a process that exited mid-scan raises. Matched by errno rather than by exception
# class: the repo's formatter rewrites a parenthesised `except (A, B):` into the 3.14-only
# bare form, and this file runs on the distro interpreter.
_GONE = (errno.ENOENT, errno.ESRCH)

# Each user the socket's group admits, mapped to the worktree root that user's scan reports
# under. Root-owned and rendered by initial_setup; lib.worktrees.holder_root reads it too.
ROOTS = Path("/etc/worktree-holders.json")

# The answer's framing. lib.worktrees.read_answer and fanout_lib.clean.live_process_scan's
# awk read the same three words.
OK = "ok"
END = "end"
REFUSED = "refused"


def escape(value: str) -> str:
    """`value` as printable ASCII on one line with no tab, which `unicode_escape` decodes back.

    A surrogate that `os.readlink` made of a byte that is not UTF-8 escapes as `\\udcNN` and
    round-trips. Each character escapes on its own, so the escaped form of a path inside a
    tree starts with the escaped tree and a `/`, which the shell caller's awk relies on.
    """
    return value.encode("unicode_escape").decode("ascii")


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
                held = os.fsdecode(var.partition(b"=")[2])
                # A NUL cannot occur in an environ entry, and a path that holds one cannot
                # resolve, so `held` always reaches `_inside` intact.
                if held and _inside(held, root):
                    found.append((pid, "CLAUDE_PROJECT_DIR", held))
                break
    return found


def root_for(user: str, roots: Path = ROOTS) -> Path | None:
    """The worktree root `roots` maps `user` to, or None when it maps no absolute path."""
    try:
        text = roots.read_text()
    except OSError:
        return None
    try:
        mapping = json.loads(text)
    except ValueError:
        return None
    held = mapping.get(user) if isinstance(mapping, dict) else None
    if not isinstance(held, str) or not held.startswith("/"):
        return None
    return Path(held)


def render(found: list[tuple[int, str, str]]) -> str:
    """One escaped line per holder `scan` found, without the framing."""
    return "".join(f"{pid}\t{kind}\t{escape(value)}\n" for pid, kind, value in found)


def answer(uid: int, roots: Path = ROOTS, proc: Path = Path("/proc")) -> str:
    """The whole answer for a caller running as `uid`: framed holders, or one refusal line."""
    try:
        user = pwd.getpwuid(uid).pw_name
    except KeyError:
        user = ""
    root = root_for(user, roots) if user else None
    if root is None:
        reason = f"{roots} maps no worktree root for uid {uid}"
        print(f"worktree-holders: refused: {reason}", file=sys.stderr)
        return f"{REFUSED}\t{escape(reason)}\n"
    return f"{OK}\n{render(scan(root, proc))}{END}\n"


def peer_uid(conn: socket.socket) -> int:
    """The uid the kernel recorded for the process at the other end of `conn`."""
    creds = conn.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
    )
    return struct.unpack("3i", creds)[1]


def serve(conn: socket.socket, roots: Path = ROOTS, proc: Path = Path("/proc")) -> None:
    """Send `conn`'s caller its answer. The caller sends nothing, and nothing is read."""
    conn.sendall(answer(peer_uid(conn), roots, proc).encode("ascii"))


def main(
    argv: list[str], roots: Path = ROOTS, proc: Path = Path("/proc"), fd: int = 0
) -> int:
    if len(argv) > 1:
        print("worktree-holders takes no arguments", file=sys.stderr)
        return 2
    try:
        conn = socket.socket(fileno=fd)
    except OSError as e:
        print(
            "worktree-holders runs only from worktree-holders.socket, with the connection "
            f"on stdin ({e.strerror})",
            file=sys.stderr,
        )
        return 2
    with conn:
        if conn.family != socket.AF_UNIX:
            print("worktree-holders answers only a unix socket", file=sys.stderr)
            return 2
        serve(conn, roots, proc)
    return 0


if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
    sys.exit(main(sys.argv))
