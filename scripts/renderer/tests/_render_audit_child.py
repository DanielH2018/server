"""The subprocess of R1's determinism check: an audit hook, then one render or one fixture step.

`_render_audit.py` runs this file once per render. Before it imports the renderer, it installs
an audit hook. The hook ends the process on any `open` outside the repo, the output directory,
the stub file and the interpreter's own prefixes, on any `socket.connect`, and on any program
start but the two probes ansible-core's import runs. The hook lives in a subprocess because
Python cannot remove an audit hook, and one installed in an xdist worker would outlive the test.

Its one argument is a JSON object naming the paths to allow and the step to run: `render`,
or the fixture steps `open`, `connect`, `spawn` and `process` that the red-proofs use. The
file imports only the standard library, so nothing from the repo loads before the hook. It
needs `scripts/` on `PYTHONPATH` to import the renderer, which `_render_audit.run_step` sets.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

# The subprocess's exit code when the hook stopped it.
AUDIT_REFUSED = 3
# subprocess opens /dev/null for the unused streams of the import probes below. The device
# holds no host state.
DEVICES = frozenset({os.devnull})
# The programs ansible-core's import runs, as (program name, arguments): `ip link` from
# `ansible/utils/vars.py`'s `uuid.getnode()`, and `ldconfig -p` from `ctypes.util.find_library`
# in `ansible/utils/display.py` and `ansible/_internal/_encryption/_crypt.py`. They read the
# host's MAC address and library paths, which no template reads. The name is matched without
# its directory, because uuid searches PATH plus /sbin and /usr/sbin for `ip`. A host without
# `ip` falls back to `ifconfig`, which the hook refuses: the check fails closed there.
IMPORT_PROBES = frozenset({("ip", ("link",)), ("ldconfig", ("-p",))})
# Each audit event that starts a program, and the index of its argv among the event's args.
# One start can raise several: `subprocess.Popen` goes on to `_posixsubprocess.fork_exec` or
# `os.posix_spawn`, so each judges argv. `fork_exec` is also the only event a multiprocessing
# or ProcessPoolExecutor start raises, and the interpreter it launches carries no hook.
SPAWN_ARGV = {
    "subprocess.Popen": 1,
    "_posixsubprocess.fork_exec": 1,
    "os.posix_spawn": 1,
    "os.exec": 1,
    "os.spawn": 2,
    "os.system": 0,
}


def _under(path: str, roots: list[str]) -> bool:
    return any(path == root or path.startswith(root + os.sep) for root in roots)


def _argv(raw) -> list[str]:
    """An audit event's argv as strings. A lone string is a program name or a shell command."""
    if raw is None:
        return []
    if isinstance(raw, (str, bytes, os.PathLike)):
        return [os.fsdecode(raw)]
    return [os.fsdecode(arg) for arg in raw]


def is_import_probe(argv: list[str]) -> bool:
    """Whether `argv` is exactly one of `IMPORT_PROBES`, wherever its program lives."""
    return bool(argv) and (os.path.basename(argv[0]), tuple(argv[1:])) in IMPORT_PROBES


def install_audit_hook(allowed: list[str]) -> None:
    """End this process on an `open` outside `allowed`, a `socket.connect` or a program start.

    A program start passes only when it is one of `IMPORT_PROBES`, since a child process
    reads the host without an `open` in this process.

    The hook exits through `os._exit`, not by raising, so a caller that catches `OSError` and
    carries on cannot hide the read. A file descriptor passed to `open` is allowed, because
    its path was checked when it was opened. The hook judges where a path resolves, never
    where it is spelled, so a symlink inside the repo that points at a host file is refused.
    """
    roots = sorted({os.path.realpath(p) for p in allowed})

    def refuse(what: str) -> None:
        os.write(2, f"render audit: refused {what}\n".encode())
        os._exit(AUDIT_REFUSED)

    def hook(event: str, args: tuple) -> None:
        if event == "socket.connect":
            refuse(f"socket.connect to {args[1]!r}")
        if event in SPAWN_ARGV:
            argv = _argv(args[SPAWN_ARGV[event]])
            if not is_import_probe(argv):
                refuse(f"{event} of {argv!r}")
            return
        if event != "open" or isinstance(args[0], int) or args[0] is None:
            return
        path = os.fsdecode(args[0])
        if path in DEVICES:
            return
        if not _under(os.path.realpath(path), roots):
            refuse(f"open of {path}")

    sys.addaudithook(hook)


def _main(spec: dict) -> int:
    install_audit_hook(
        [
            *spec["allow"],
            sys.prefix,
            sys.base_prefix,
            sys.exec_prefix,
            sys.base_exec_prefix,
        ]
    )
    step = spec["step"]
    if step == "open":
        with open(spec["path"], "rb"):
            return 0
    if step == "connect":
        import socket

        with socket.socket() as sock:
            sock.connect((spec["host"], spec["port"]))
        return 0
    if step == "spawn":
        # Not lib.proc_testing.run: nothing from the repo loads before the hook. The bound
        # stops a wedged program the hook let through.
        return subprocess.run(spec["argv"], check=False, timeout=30).returncode
    if step == "process":
        import multiprocessing

        child = multiprocessing.get_context("spawn").Process(target=os.getpid)
        child.start()
        child.join(30)
        return 0
    from renderer import homelab_render

    return homelab_render.cluster(Path(spec["out"]), spec["tags"], Path(spec["stubs"]))


if __name__ == "__main__":
    sys.exit(_main(json.loads(sys.argv[1])))
