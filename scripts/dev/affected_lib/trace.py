"""Read which repository paths one traced process tree touched, from an `strace` log.

`affected_tests.py record` runs each test module alone under `strace -f -y` and hands the log
here. The kernel's view covers what no in-process hook reports: a child `bash` or `git`, a
`stat` that only asks whether a path exists, and a directory listing.

Three kinds of touch come back, because a change reaches a test in three ways:

- `reads`: a path a syscall resolved. Editing or deleting it can change the test.
- `missing`: a path a syscall asked for and did not find. Creating it can change the test.
- `listed`: a directory whose entries a process read. Adding or removing an entry can change
  the test.

A relative path is resolved against the directory `strace -y` prints for `AT_FDCWD`. The older
syscalls that take no directory descriptor (`stat`, `open`, `access`) use the working directory
this module tracks per process from `chdir`, `fchdir` and the `AT_FDCWD` annotations.
"""

import os.path
import re
from dataclasses import dataclass, field

# The fork family is traced only so a child inherits its parent's working directory.
SYSCALLS = (
    "open,openat,openat2,stat,lstat,newfstatat,statx,access,faccessat,faccessat2,"
    "readlink,readlinkat,getdents64,execve,execveat,chdir,fchdir,clone,clone3,fork,vfork"
)

_PATH_FIRST = frozenset(
    {"open", "stat", "lstat", "access", "readlink", "execve", "chdir"}
)
_PATH_AFTER_DIRFD = frozenset(
    {
        "openat",
        "openat2",
        "newfstatat",
        "statx",
        "faccessat",
        "faccessat2",
        "readlinkat",
        "execveat",
    }
)
_FORKS = frozenset({"clone", "clone3", "fork", "vfork"})

_LINE = re.compile(r"^(\d+) +(.*)$")
_CALL = re.compile(r"^(\w+)\((.*)$", re.DOTALL)
_RESUMED = re.compile(r"^<\.\.\. (\w+) resumed>(.*)$", re.DOTALL)
_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
_FD = re.compile(r"^(?:AT_FDCWD|-?\d+)(?:<((?:[^>\\]|\\.)*)>)?")
_RESULT = re.compile(r"\) = (-?\d+)(?:<((?:[^>\\]|\\.)*)>)?(?: (E[A-Z]+) \(.*\))?\s*$")
_ESCAPE = re.compile(r"\\([0-7]{1,3}|x[0-9a-fA-F]{2}|.)")
_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "v": "\v", "f": "\f"}


def _unescape(text: str) -> str:
    """The path a strace string literal stands for."""
    if "\\" not in text:
        return text

    def one(match: re.Match[str]) -> bytes:
        body = match.group(1)
        if body[0] in "01234567":
            return bytes([int(body, 8) & 0xFF])
        if body[0] == "x" and len(body) == 3:
            return bytes([int(body[1:], 16)])
        return _SIMPLE_ESCAPES.get(body, body).encode()

    parts, at = [], 0
    for match in _ESCAPE.finditer(text):
        parts.append(text[at : match.start()].encode())
        parts.append(one(match))
        at = match.end()
    parts.append(text[at:].encode())
    return b"".join(parts).decode(errors="surrogateescape")


@dataclass
class Touched:
    """What one process tree touched, as absolute normalised paths.

    Attributes:
        reads: Paths a syscall resolved.
        missing: Paths a syscall did not find.
        listed: Directories whose entries each process read, keyed by process id.
        root_pid: The first process in the log, which is the traced command itself.
    """

    reads: set[str] = field(default_factory=set)
    missing: set[str] = field(default_factory=set)
    listed: dict[int, set[str]] = field(default_factory=dict)
    root_pid: int | None = None


def _joined(lines):
    """Yield `(pid, call text)` for each finished syscall, joining a split one."""
    unfinished: dict[int, str] = {}
    for raw in lines:
        match = _LINE.match(raw.rstrip("\n"))
        if not match:
            continue
        pid, rest = int(match.group(1)), match.group(2)
        if rest.endswith("<unfinished ...>"):
            unfinished[pid] = rest[: -len("<unfinished ...>")]
            yield pid, rest, False
            continue
        if resumed := _RESUMED.match(rest):
            rest = unfinished.pop(pid, f"{resumed.group(1)}(") + resumed.group(2)
        yield pid, rest, True


def parse(lines, start_dir: str) -> Touched:
    """Collect every path the traced tree touched.

    Args:
        lines: The lines of an `strace -f -y` log written with `-o`, so each starts with a
            process id.
        start_dir: The working directory the traced command started in.
    """
    touched = Touched()
    cwd: dict[int, str] = {}
    forking_cwd = start_dir

    def directory_of(pid: int) -> str:
        return cwd.setdefault(pid, forking_cwd)

    for pid, text, finished in _joined(lines):
        if touched.root_pid is None:
            touched.root_pid = pid
        call = _CALL.match(text)
        if not call:
            continue
        name, args = call.group(1), call.group(2)
        if name in _FORKS:
            forking_cwd = directory_of(pid)
            continue
        if not finished:
            continue
        result = _RESULT.search(args)
        if not result:
            continue
        returned, returned_path, error = result.groups()

        base = directory_of(pid)
        if name == "getdents64" or name == "fchdir":
            fd = _FD.match(args)
            if not (fd and fd.group(1)):
                continue
            path = os.path.normpath(_unescape(fd.group(1)))
            if name == "fchdir":
                if returned == "0":
                    cwd[pid] = path
            elif not error:
                touched.listed.setdefault(pid, set()).add(path)
            continue

        if name in _PATH_AFTER_DIRFD:
            fd = _FD.match(args)
            if not fd:
                continue
            if fd.group(1):
                base = os.path.normpath(_unescape(fd.group(1)))
                if args.startswith("AT_FDCWD"):
                    cwd[pid] = base
            elif not args.startswith("AT_FDCWD"):
                base = ""
            args = args[fd.end() :]
        elif name not in _PATH_FIRST:
            continue
        string = _STRING.search(args)
        if not string:
            continue
        given = _unescape(string.group(1))
        if not given.startswith("/"):
            if not base:
                continue
            given = os.path.join(base, given)
        path = os.path.normpath(given)

        if error in ("ENOENT", "ENOTDIR"):
            touched.missing.add(path)
        elif not error:
            touched.reads.add(path)
            if returned_path and returned_path.startswith("/"):
                touched.reads.add(os.path.normpath(_unescape(returned_path)))
            if name == "chdir":
                cwd[pid] = path
    return touched
