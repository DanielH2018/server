"""The subprocess of R1's determinism check: an audit hook, then one render or one fixture step.

`_render_audit.py` runs this file once per render. Before it imports the renderer, it installs
an audit hook. The hook ends the process on any `open` outside the repo, the output directory,
the stub file and the interpreter's own prefixes, and on any `socket.connect`. The hook lives in a subprocess because Python cannot remove an audit hook,
and one installed in an xdist worker would outlive the test.

Its one argument is a JSON object naming the paths to allow and the step to run: `render`,
or the fixture steps `open` and `connect` that the red-proofs use. The file imports only the
standard library, so nothing from the repo loads before the hook. It needs `scripts/` on
`PYTHONPATH` to import the renderer, which `_render_audit.run_step` sets.
"""

import json
import os
import sys
from pathlib import Path

# The subprocess's exit code when the hook stopped it.
AUDIT_REFUSED = 3
# ansible-core's import runs `ldconfig -p` and `ip link` (ctypes.util.find_library and
# uuid.getnode), and subprocess opens /dev/null for their unused streams. The device holds no
# host state. The probes themselves are processes, which an `open` hook does not see.
DEVICES = frozenset({os.devnull})


def _under(path: str, roots: list[str]) -> bool:
    return any(path == root or path.startswith(root + os.sep) for root in roots)


def install_audit_hook(allowed: list[str]) -> None:
    """End this process on an `open` outside `allowed` or on any `socket.connect`.

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
    from renderer import homelab_render

    return homelab_render.cluster(Path(spec["out"]), spec["tags"], Path(spec["stubs"]))


if __name__ == "__main__":
    sys.exit(_main(json.loads(sys.argv[1])))
