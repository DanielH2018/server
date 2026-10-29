#!/usr/bin/python3 -I
"""homelab-rootq-socket: answer one `rootq` request on a unix socket, as root (#6350).

Installed root-owned at /usr/local/libexec/homelab-rootq-socket by tasks/rootq.yml and run by
`homelab-rootq@.service`, one instance per connection to `homelab-rootq.socket`
(`Accept=yes`), with the connection on stdin. The socket's group decides who may connect. A
socket needs no setuid, so the agent user, whose `claude-rc.service` runs with
`NoNewPrivileges=yes`, can ask it where `sudo` would fail (#4297). The operator's sudo path
to the same script (`/usr/local/bin/rootq`) is unchanged.

The helper adds no policy of its own to what rootq reads. It runs the installed
/usr/local/sbin/homelab-rootq by absolute path, so the verbs, the path allowlist, the deny
list and the redaction are that script's own, and the caller gets its refusal text.

Request: one line of printable ASCII, at most REQUEST_MAX bytes with the newline, and
nothing after the newline:

    VERB [PATH [HEX]]

A single space separates the words. The verb is one of `ARITY`, which takes the argument
count shown there, or the one word `--help`. `matches` takes PATH and the 64-hex SHA-256 the
client computed, so root never opens a caller-named file. A PATH is absolute, uses only the
characters rootq accepts, and holds no empty, `.` or `..` component. Anything else is refused
here, before rootq runs. Whether a PATH is on the allowlist is rootq's decision alone.

Answer: one JSON object on one line, `{"exit": N, "stdout": "...", "stderr": "..."}`. A
socket carries no exit status, so the caller treats an answer that is missing, truncated or
not that object as a failure. A refusal made here has exit 2 and the text `refused: ...`
that rootq's own refusals use.

`#!/usr/bin/python3 -I`: the distro interpreter in isolated mode, so no `PYTHON*` variable
and no user site directory reaches a root process. The helper never starts a shell.
"""

import json
import re
import socket
import struct
import subprocess
import sys
import time

SCRIPT = "/usr/local/sbin/homelab-rootq"
# `rootq.TIER1`, `rootq.COMMANDS` and `cat`, each with its argument count. A test holds this
# equal to the script's own verb set.
ARITY = {
    "stat": 1,
    "sha256": 1,
    "matches": 2,
    "cat": 1,
    "nft": 0,
    "ufw": 0,
    "sshd": 0,
    "crontab": 0,
}
# `matches ` + a 4096-character PATH (rootq's bound) + a space + 64 hex + the newline = 4170.
REQUEST_MAX = 4200
READ_TIMEOUT_S = 5
# Above rootq's own 30 s limit on a command it runs, and below the unit's RuntimeMaxSec.
RUN_TIMEOUT_S = 45
# rootq caps each stream at 256 KiB; this bound holds even if that one moved.
STREAM_MAX = 512 * 1024
PATH_SHAPE = re.compile(r"/[A-Za-z0-9._@+:,=/-]{0,4095}")
HEX64 = re.compile(r"[0-9a-f]{64}")
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}
# `lib/exit_codes.py:USAGE_ERROR`, which this host-shipped script cannot import (#4487).
USAGE_ERROR = 64
REFUSED = 2


class Refused(Exception):
    pass


def read_request(conn: socket.socket, timeout_s: float = READ_TIMEOUT_S) -> bytes:
    """The one request line, without its newline. Reads at most REQUEST_MAX + 1 bytes."""
    deadline = time.monotonic() + timeout_s
    data = b""
    while b"\n" not in data:
        if len(data) > REQUEST_MAX:
            raise Refused(f"the request is over {REQUEST_MAX} bytes")
        # One deadline for the whole line, so a caller sending a byte at a time cannot hold
        # the instance for REQUEST_MAX timeouts.
        left = deadline - time.monotonic()
        if left <= 0:
            raise Refused(f"no request line within {timeout_s}s")
        conn.settimeout(left)
        try:
            chunk = conn.recv(REQUEST_MAX + 1 - len(data))
        except TimeoutError:
            raise Refused(f"no request line within {timeout_s}s") from None
        if not chunk:
            raise Refused("the request ended before its newline")
        data += chunk
    line, _, rest = data.partition(b"\n")
    if len(line) + 1 > REQUEST_MAX:
        raise Refused(f"the request is over {REQUEST_MAX} bytes")
    if rest:
        raise Refused("send one request line and nothing after it")
    return line


def check_path(path: str) -> None:
    if not PATH_SHAPE.fullmatch(path):
        raise Refused(
            "PATH must be absolute and use only letters, digits and . _ @ + : , = / -"
        )
    if any(part in ("", ".", "..") for part in path.split("/")[1:]):
        raise Refused(f"{path} holds an empty, . or .. component")


def parse(line: bytes) -> list[str]:
    """The argv rootq receives for `line`, or Refused. Nothing here consults the allowlist."""
    try:
        text = line.decode("ascii")
    except UnicodeDecodeError:
        raise Refused("the request must be printable ASCII") from None
    if not all(" " <= char <= "~" for char in text):
        raise Refused("the request must be printable ASCII on one line")
    words = text.split(" ")
    if "" in words:
        raise Refused("separate the words with single spaces")
    verb, args = words[0], words[1:]
    if words == ["--help"]:
        return words
    if verb not in ARITY:
        raise Refused(f"unknown verb {verb!r}; one of {' '.join(ARITY)}")
    if len(args) != ARITY[verb]:
        raise Refused(f"{verb} takes {ARITY[verb]} argument(s), not {len(args)}")
    if args:
        check_path(args[0])
    if verb == "matches" and not HEX64.fullmatch(args[1]):
        raise Refused("matches takes PATH and the 64-hex SHA-256 the client computed")
    return words


def reply(code: int, out: str = "", err: str = "") -> str:
    return json.dumps({"exit": code, "stdout": out, "stderr": err}) + "\n"


def run_rootq(
    argv: list[str],
    script: tuple[str, ...] = (SCRIPT,),
    env: dict[str, str] | None = None,
    runner=subprocess.run,
) -> str:
    """rootq's answer for `argv`: the script run by absolute path, no shell, stdin closed.

    `runner` is a seam for a test that records how the script is started.
    """
    try:
        done = runner(
            [*script, *argv],
            env=ENV if env is None else env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=RUN_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return reply(1, err=f"rootq ran past {RUN_TIMEOUT_S}s\n")
    except OSError as err:
        return reply(3, err=f"cannot run {script[-1]}: {err.strerror}\n")
    return reply(
        done.returncode,
        done.stdout[:STREAM_MAX].decode("utf-8", "replace"),
        done.stderr[:STREAM_MAX].decode("utf-8", "replace"),
    )


def peer_uid(conn: socket.socket) -> int:
    creds = conn.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
    )
    return struct.unpack("3i", creds)[1]


def serve(
    conn: socket.socket,
    script: tuple[str, ...] = (SCRIPT,),
    env: dict[str, str] | None = None,
    timeout_s: float = READ_TIMEOUT_S,
) -> None:
    """Read `conn`'s request, run it through rootq and send the answer."""
    try:
        argv = parse(read_request(conn, timeout_s))
    except Refused as err:
        # The journal keeps the reason and the caller's uid, never the request's bytes.
        print(f"homelab-rootq-socket: uid {peer_uid(conn)} refused: {err}")
        answer = reply(REFUSED, err=f"refused: {err}\n")
    else:
        print(f"homelab-rootq-socket: uid {peer_uid(conn)} asked: {' '.join(argv)}")
        answer = run_rootq(argv, script, env)
    conn.settimeout(timeout_s)
    conn.sendall(answer.encode("ascii"))


def main(argv: list[str], fd: int = 0) -> int:
    if len(argv) > 1:
        print("homelab-rootq-socket takes no arguments", file=sys.stderr)
        return USAGE_ERROR
    try:
        conn = socket.socket(fileno=fd)
    except OSError as err:
        print(
            "homelab-rootq-socket runs only from homelab-rootq.socket, with the "
            f"connection on stdin ({err.strerror})",
            file=sys.stderr,
        )
        return USAGE_ERROR
    with conn:
        if conn.family != socket.AF_UNIX:
            print("homelab-rootq-socket answers only a unix socket", file=sys.stderr)
            return USAGE_ERROR
        serve(conn)
    return 0


if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
    sys.exit(main(sys.argv))
