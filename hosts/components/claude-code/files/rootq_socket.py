#!/usr/bin/python3 -I
"""rootq-socket: read root-only host state through homelab-rootq.socket (#6350).

Installed as /usr/local/bin/rootq-socket by tasks/rootq.yml, for a user in the socket's group
that has no sudo, such as the agent user. The operator's `rootq` takes the same verbs through
sudo.

Usage: rootq-socket stat PATH | sha256 PATH | matches PATH REPO_FILE|HEX   (tier 1, no content)
       rootq-socket nft | ufw | sshd | crontab | cat PATH                  (tier 2, redacted)

The root helper runs the same script as `rootq` and refuses what that script refuses, so
nothing is validated here beyond `matches`. This client hashes REPO_FILE as the caller and
sends only the hash, so root never opens a caller-named file. REPO_FILE must be relative, hold
no `..` and resolve inside the checkout around the working directory. A 64-hex SHA-256 in its
place goes to root as it is. HOMELAB_ROOTQ_SOCKET names another socket for a test; this
client runs as the caller, so naming one gains nothing.

Exit status: rootq's own (0 and 1 for match and differ, 2 refused, 3 missing command), 69
when the socket cannot be reached, 70 when the answer is missing or malformed.
"""

import hashlib
import json
import os
import re
import socket
import subprocess
import sys

SOCKET = "/run/homelab-rootq.sock"
HEX64 = re.compile(r"[0-9a-f]{64}")
ANSWER_MAX = 4 * 1024 * 1024
TIMEOUT_S = 60
UNREACHABLE = 69
BAD_ANSWER = 70


def refuse(reason: str) -> int:
    print(f"refused: {reason}", file=sys.stderr)
    return 2


def hash_repo_file(repo_file: str) -> str:
    """The SHA-256 of REPO_FILE in the caller's checkout, or ValueError naming why not."""
    if HEX64.fullmatch(repo_file):
        return repo_file
    if repo_file.startswith("/") or ".." in repo_file.split("/"):
        raise ValueError("REPO_FILE must be relative and hold no ..")
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if top.returncode:
        raise ValueError("run matches inside a checkout")
    try:
        real = os.path.realpath(repo_file, strict=True)
    except OSError:
        raise ValueError(f"{repo_file} does not exist") from None
    if not real.startswith(top.stdout.strip() + "/"):
        raise ValueError(f"{repo_file} resolves outside the checkout")
    if not os.path.isfile(real):
        raise ValueError(f"{repo_file} is not a regular file")
    with open(real, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def ask(path: str, line: str) -> bytes:
    """The helper's whole answer to `line`, read to the end of the connection."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(TIMEOUT_S)
        conn.connect(path)
        conn.sendall(line.encode() + b"\n")
        conn.shutdown(socket.SHUT_WR)
        data = b""
        while len(data) <= ANSWER_MAX:
            try:
                chunk = conn.recv(65536)
            except OSError:
                # A reset or a timeout mid-answer leaves a partial answer, which decode()
                # refuses; it is not the same fault as a socket that cannot be reached.
                break
            if not chunk:
                break
            data += chunk
    return data


def decode(data: bytes) -> tuple[int, str, str] | None:
    """(exit, stdout, stderr) from a whole answer, or None for anything else."""
    try:
        body = json.loads(data)
    except ValueError:
        return None
    if not isinstance(body, dict) or not data.endswith(b"\n"):
        return None
    code, out, err = body.get("exit"), body.get("stdout"), body.get("stderr")
    if not (isinstance(code, int) and isinstance(out, str) and isinstance(err, str)):
        return None
    return code, out, err


def main(argv: list[str], environ: dict[str, str]) -> int:
    if argv in (["-h"], ["--help"]):
        print(__doc__.split("\n\n", 1)[1].rstrip())
        return 0
    args = list(argv)
    if args[:1] == ["matches"] and len(args) == 3:
        try:
            args[2] = hash_repo_file(args[2])
        except ValueError as err:
            return refuse(str(err))
    path = environ.get("HOMELAB_ROOTQ_SOCKET", SOCKET)
    try:
        data = ask(path, " ".join(args))
    except OSError as err:
        print(
            f"rootq-socket: cannot reach {path}: {err.strerror}. Your user must be in "
            "the socket's group, and a session started before it joined must restart.",
            file=sys.stderr,
        )
        return UNREACHABLE
    answer = decode(data)
    if answer is None:
        print("rootq-socket: the answer was missing or malformed", file=sys.stderr)
        return BAD_ANSWER
    code, out, err = answer
    sys.stdout.write(out)
    sys.stderr.write(err)
    return code if 0 <= code <= 255 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], dict(os.environ)))
